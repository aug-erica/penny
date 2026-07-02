"""Card-feed adapter (production): read Chase card charges from QuickBooks Online
via API, replacing the manual CSV.

MECHANISM (verified July 2026 — see QuickBooks-API-Setup-Plan.md): the QBO API
does NOT expose "For Review" bank-feed items, only POSTED transactions. Once the
card is bank-fed to QBO (not yet — today it's Expensify->QBO at close), charges
post to the CC register as `Purchase` entities and we read them:
    SELECT * FROM Purchase WHERE AccountRef = '<cc_account_id>'
      AND TxnDate >= '<start>' AND TxnDate <= '<end>'

Auth: OAuth2. Access token ~1h; refresh token ~100d and ROTATES on every use —
we persist the rotated refresh token back to .env so it never goes stale.

STATUS: auth + query + CC-account listing are implemented and testable via
`cfo qbo smoke` against the sandbox. The Purchase->CardTxn merchant-field
mapping is best-effort and must be confirmed against real bank-fed data (the
raw Chase descriptor's exact field isn't known until we see live fed charges).
"""
from __future__ import annotations

import base64
from datetime import date, datetime
from pathlib import Path
from typing import List

import httpx

from ...config import env
from ...models import CardTxn, StatementCheck

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
BASES = {"sandbox": "https://sandbox-quickbooks.api.intuit.com",
         "production": "https://quickbooks.api.intuit.com"}
MINOR_VERSION = 73
ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


class QBOError(RuntimeError):
    pass


class QBOFeed:
    def __init__(self, account_ref: str = "", realm_id: str = "", **_):
        self.account_ref = account_ref or env("QBO_ACCOUNT_REF")  # CC account id
        self.realm_id = realm_id or env("QBO_REALM_ID")
        self.base = BASES.get(env("QBO_ENV") or "sandbox", BASES["sandbox"])
        self._access_token = None

    # -- auth ----------------------------------------------------------------
    def _refresh_access_token(self) -> str:
        cid, secret = env("QBO_CLIENT_ID"), env("QBO_CLIENT_SECRET")
        refresh = env("QBO_REFRESH_TOKEN")
        if not (cid and secret and refresh):
            raise QBOError("Missing QBO_CLIENT_ID / QBO_CLIENT_SECRET / QBO_REFRESH_TOKEN "
                           "in .env — run the OAuth Playground to get the refresh token.")
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        resp = httpx.post(TOKEN_URL, headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }, data={"grant_type": "refresh_token", "refresh_token": refresh}, timeout=60)
        if resp.status_code != 200:
            raise QBOError(f"Token refresh failed: HTTP {resp.status_code} {resp.text[:300]}")
        tok = resp.json()
        # Refresh tokens rotate — persist the new one so it doesn't expire on us.
        new_refresh = tok.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            self._persist_refresh_token(new_refresh)
        self._access_token = tok["access_token"]
        return self._access_token

    @staticmethod
    def _persist_refresh_token(new_refresh: str):
        if not ENV_PATH.exists():
            return
        lines = ENV_PATH.read_text().splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("QBO_REFRESH_TOKEN="):
                lines[i] = f"QBO_REFRESH_TOKEN={new_refresh}"
        ENV_PATH.write_text("\n".join(lines) + "\n")

    # -- query ---------------------------------------------------------------
    def query(self, sql: str) -> list:
        if not self._access_token:
            self._refresh_access_token()
        if not self.realm_id:
            raise QBOError("Missing QBO_REALM_ID — comes from the OAuth Playground.")
        url = f"{self.base}/v3/company/{self.realm_id}/query"
        resp = httpx.get(url, params={"query": sql, "minorversion": MINOR_VERSION},
                         headers={"Authorization": f"Bearer {self._access_token}",
                                  "Accept": "application/json"}, timeout=90)
        if resp.status_code == 401:  # access token expired mid-run — refresh once
            self._refresh_access_token()
            resp = httpx.get(url, params={"query": sql, "minorversion": MINOR_VERSION},
                             headers={"Authorization": f"Bearer {self._access_token}",
                                      "Accept": "application/json"}, timeout=90)
        if resp.status_code != 200:
            raise QBOError(f"Query failed: HTTP {resp.status_code} {resp.text[:300]}")
        qr = resp.json().get("QueryResponse", {})
        # QueryResponse holds the entity under its type key (Account, Purchase, …)
        for k, v in qr.items():
            if isinstance(v, list):
                return v
        return []

    def credit_card_accounts(self) -> list:
        rows = self.query("SELECT * FROM Account WHERE AccountType = 'CreditCard'")
        return [{"id": a["Id"], "name": a.get("FullyQualifiedName") or a.get("Name")}
                for a in rows]

    # -- adapter interface ---------------------------------------------------
    def fetch_transactions(self, month_start: date, month_end: date) -> List[CardTxn]:
        accts = ([{"id": self.account_ref, "name": ""}] if self.account_ref
                 else self.credit_card_accounts())
        out = []
        for a in accts:
            sql = (f"SELECT * FROM Purchase WHERE AccountRef = '{a['id']}' "
                   f"AND TxnDate >= '{month_start}' AND TxnDate <= '{month_end}' "
                   f"AND PaymentType = 'CreditCard' MAXRESULTS 1000")
            for p in self.query(sql):
                out.append(self._to_cardtxn(p, a["id"]))
        return out

    @staticmethod
    def _to_cardtxn(p: dict, account_id: str) -> CardTxn:
        cents = round(float(p.get("TotalAmt", 0)) * 100)
        if p.get("Credit"):          # a credit/refund on the card
            cents = -cents
        # Merchant field is best-effort until confirmed on real fed data: prefer
        # the payee (EntityRef), fall back to the memo / first line description.
        merchant = ((p.get("EntityRef") or {}).get("name")
                    or p.get("PrivateNote")
                    or (p.get("Line", [{}])[0].get("Description") if p.get("Line") else "")
                    or "UNKNOWN")
        return CardTxn(
            txn_date=datetime.strptime(p["TxnDate"], "%Y-%m-%d").date(),
            merchant_raw=merchant.strip(),
            amount_cents=cents,
            cardholder=None,          # QBO posts to the account; cardholder not on the txn
            statement_ref=f"qbo:{account_id}",
        )

    def verify(self) -> List[StatementCheck]:
        # No printed control total on a live read; completeness cross-checks
        # against the QBO rec report at close (books_out).
        return []
