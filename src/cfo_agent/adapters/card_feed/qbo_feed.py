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
    @staticmethod
    def _load_refresh_token() -> str:
        """Latest rotated token from the KV store (survives Railway restarts);
        the .env value is the seed / local fallback."""
        try:
            from ...engine import kv
            tok = kv.get("qbo_refresh_token")
            if tok:
                return tok
        except Exception:
            pass
        return env("QBO_REFRESH_TOKEN")

    def _refresh_access_token(self) -> str:
        from datetime import datetime, timedelta, timezone

        from ...engine import kv
        # Reuse a shared, unexpired ACCESS token (valid ~1h) from the KV store
        # instead of calling the refresh endpoint every time. Each refresh ROTATES
        # the refresh token, and doing that per-call caused invalid_grant races
        # between the poller, reply handler, and local scripts. Now we rotate at
        # most ~hourly, when the cached access token is actually expired.
        cached, exp = kv.get("qbo_access_token"), kv.get("qbo_access_expiry")
        if cached and exp:
            try:
                if datetime.fromisoformat(exp) > datetime.now(timezone.utc) + timedelta(minutes=2):
                    self._access_token = cached
                    return cached
            except ValueError:
                pass
        cid, secret = env("QBO_CLIENT_ID"), env("QBO_CLIENT_SECRET")
        refresh = self._load_refresh_token()
        seed = env("QBO_REFRESH_TOKEN")           # the .env / env-var seed token
        if not (cid and secret and refresh):
            raise QBOError("Missing QBO_CLIENT_ID / QBO_CLIENT_SECRET / QBO_REFRESH_TOKEN "
                           "in .env — run the OAuth Playground to get the refresh token.")
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()

        def _hit(rt: str) -> httpx.Response:
            return httpx.post(TOKEN_URL, headers={
                "Authorization": f"Basic {basic}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }, data={"grant_type": "refresh_token", "refresh_token": rt}, timeout=60)

        resp = _hit(refresh)
        # SELF-HEAL: if the stored (rotated) token is dead — e.g. killed by a
        # concurrency race — fall back to the env-var seed. Recovery is then just
        # "set a fresh QBO_REFRESH_TOKEN on the service"; no manual DB surgery.
        if (resp.status_code != 200 and "invalid_grant" in resp.text
                and seed and seed != refresh):
            print("⚠  stored QBO refresh token rejected — retrying with env-var seed",
                  flush=True)
            resp = _hit(seed)
        if resp.status_code != 200:
            raise QBOError(f"Token refresh failed: HTTP {resp.status_code} {resp.text[:300]}")
        tok = resp.json()
        # Refresh tokens rotate — persist the new one so it doesn't expire on us.
        new_refresh = tok.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            self._persist_refresh_token(new_refresh)
        self._access_token = tok["access_token"]
        try:                                   # cache the access token for reuse
            kv.set("qbo_access_token", self._access_token)
            kv.set("qbo_access_expiry", (datetime.now(timezone.utc)
                    + timedelta(seconds=int(tok.get("expires_in", 3600)) - 120)).isoformat())
        except Exception:
            pass
        return self._access_token

    @staticmethod
    def _persist_refresh_token(new_refresh: str):
        # Primary store is the DB (KV -> Postgres in the cloud): Railway's
        # filesystem is ephemeral, so a rotated token written only to .env is
        # lost on restart and auth dies. Never fail the run over persistence —
        # but say so loudly, because a lost rotation is a slow-motion outage.
        try:
            from ...engine import kv
            kv.set("qbo_refresh_token", new_refresh)
        except Exception as exc:
            print(f"⚠  could not persist rotated QBO refresh token to the DB: {exc}",
                  flush=True)
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
        # NB: Account.AccountType enum is "Credit Card" (with space); only
        # Purchase.PaymentType uses "CreditCard" (no space). Different enums.
        rows = self.query("SELECT * FROM Account WHERE AccountType = 'Credit Card'")
        return [{"id": a["Id"], "name": a.get("FullyQualifiedName") or a.get("Name")}
                for a in rows]

    # -- adapter interface ---------------------------------------------------
    def fetch_transactions(self, month_start: date, month_end: date) -> List[CardTxn]:
        # Purchase.AccountRef isn't queryable in WHERE, so page through all
        # purchases in the date range and keep those on our credit-card accounts.
        cc_ids = {self.account_ref} if self.account_ref else {
            a["id"] for a in self.credit_card_accounts()}
        out, start, page = [], 1, 1000
        while True:
            sql = (f"SELECT * FROM Purchase WHERE TxnDate >= '{month_start}' "
                   f"AND TxnDate <= '{month_end}' ORDER BY TxnDate "
                   f"STARTPOSITION {start} MAXRESULTS {page}")
            batch = self.query(sql)
            for p in batch:
                if (p.get("AccountRef") or {}).get("value") in cc_ids:
                    out.append(self._to_cardtxn(p, (p["AccountRef"])["value"]))
            if len(batch) < page:
                break
            start += page
        return out

    @staticmethod
    def _cardholder_from_account(account_name: str) -> str:
        # "Chase Business Card (8303):Chase Business Card - P. Patel (6761)" -> "P. Patel"
        if account_name and " - " in account_name:
            return account_name.rsplit(" - ", 1)[1].split(" (")[0].strip()
        return None  # parent/main card (8303) has no sub-name

    @staticmethod
    def _to_cardtxn(p: dict, account_id: str) -> CardTxn:
        cents = round(float(p.get("TotalAmt", 0)) * 100)
        if p.get("Credit"):          # a credit/refund on the card
            cents = -cents
        # Verified against real QBO data (2026-07-02): the merchant lives in the
        # first expense line's Description; EntityRef is a generic vendor
        # ("Credit Card Misc."), so it is NOT the merchant. Fall back to memo.
        line0 = (p.get("Line") or [{}])[0]
        merchant = (line0.get("Description")
                    or p.get("PrivateNote")
                    or (p.get("EntityRef") or {}).get("name")
                    or "UNKNOWN")
        acct = (p.get("AccountRef") or {})
        return CardTxn(
            txn_date=datetime.strptime(p["TxnDate"], "%Y-%m-%d").date(),
            merchant_raw=merchant.strip(),
            amount_cents=cents,
            cardholder=QBOFeed._cardholder_from_account(acct.get("name", "")),
            statement_ref=f"qbo:{acct.get('value') or account_id}",
        )

    def verify(self) -> List[StatementCheck]:
        # No printed control total on a live read; completeness cross-checks
        # against the QBO rec report at close (books_out).
        return []
