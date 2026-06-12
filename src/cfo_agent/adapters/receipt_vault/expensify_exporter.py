"""Receipt-vault adapter: Expensify Integration Server (Report Exporter).

Strictly read-only: no onFinish actions, no export-marking, no mutations.
API facts that matter:
- One endpoint, form-encoded POST; credentials ride inside the JSON payload.
- Two-step flow: `type: file` (with a Freemarker template) returns a generated
  filename; `type: download` fetches it.
- Amounts are integer CENTS. `modified*` fields carry reviewer corrections —
  prefer them when present.
- startDate/endDate filter on REPORT dates, not expense dates: callers pass a
  padded window and we filter by expense date downstream.
- Errors can come back as HTTP 200 with a JSON `responseCode` body — always
  inspect the body. Rate limit: 5 req/10s (irrelevant at our volume, but 429
  gets a backoff anyway).
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import List

import httpx

from ...models import VaultExpense

ENDPOINT = "https://integrations.expensify.com/Integration-Server/ExpensifyIntegrations"


class ExpensifyError(RuntimeError):
    pass


class ExpensifyExporter:
    def __init__(self, partner_user_id: str, partner_user_secret: str,
                 template_path: Path, cache_dir: Path):
        if not partner_user_id or not partner_user_secret:
            raise ExpensifyError(
                "Missing Expensify credentials — set EXPENSIFY_PARTNER_USER_ID and "
                "EXPENSIFY_PARTNER_USER_SECRET in .env (generate at "
                "expensify.com from an approver account)."
            )
        self._creds = {"partnerUserID": partner_user_id,
                       "partnerUserSecret": partner_user_secret}
        self.template = Path(template_path).read_text()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- HTTP ----------------------------------------------------------------
    def _post(self, job: dict, template: str = None) -> bytes:
        data = {"requestJobDescription": json.dumps(job)}
        if template is not None:
            data["template"] = template
        for attempt in range(4):
            resp = httpx.post(ENDPOINT, data=data, timeout=120)
            if resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code != 200:
                raise ExpensifyError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            body = resp.content
            stripped = body.strip()
            if stripped.startswith(b"{"):
                payload = json.loads(stripped)
                code = payload.get("responseCode")
                if code and code != 200:
                    raise ExpensifyError(f"Expensify error {code}: "
                                         f"{payload.get('responseMessage', body[:300])}")
            return body
        raise ExpensifyError("Rate-limited after retries")

    # -- adapter interface -----------------------------------------------------
    def fetch_expenses(self, window_start: date, window_end: date,
                       states: List[str]) -> List[VaultExpense]:
        cache = self.cache_dir / (
            f"expensify_{window_start}_{window_end}_{'-'.join(sorted(states))}.csv")
        if cache.exists():
            raw = cache.read_text()
        else:
            job = {
                "type": "file",
                "credentials": self._creds,
                "onReceive": {"immediateResponse": ["returnRandomFileName"]},
                "inputSettings": {
                    "type": "combinedReportData",
                    "filters": {
                        "startDate": window_start.isoformat(),
                        "endDate": window_end.isoformat(),
                        "reportState": ",".join(states),
                    },
                },
                "outputSettings": {"fileExtension": "csv"},
            }
            filename = self._post(job, template=self.template).decode().strip()
            if not filename or "\n" in filename or len(filename) > 200:
                raise ExpensifyError(f"Unexpected export response: {filename[:300]!r}")
            raw = self._post({
                "type": "download",
                "credentials": self._creds,
                "fileName": filename,
                "fileSystem": "integrationServer",
            }).decode()
            cache.write_text(raw)
        return self._parse_csv(raw)

    @staticmethod
    def _parse_csv(raw: str) -> List[VaultExpense]:
        out = []
        reader = csv.DictReader(io.StringIO(raw), delimiter="|")
        for row in reader:
            amount = int(row.get("modifiedAmount") or 0) or int(row.get("amount") or 0)
            merchant = (row.get("modifiedMerchant") or "").strip() or row.get("merchant", "")
            out.append(VaultExpense(
                expense_date=datetime.strptime(row["created"][:10], "%Y-%m-%d").date(),
                merchant=merchant,
                amount_cents=amount,
                currency=row.get("currency", "USD"),
                employee=row.get("submitter", ""),
                category=row.get("category", ""),
                tag=row.get("tag", ""),
                billable=row.get("billable", "").lower() == "true",
                reimbursable=row.get("reimbursable", "").lower() == "true",
                receipt_url=row.get("receiptURL", ""),
                report_id=row.get("reportID", ""),
                report_state=row.get("reportState", ""),
                vault_id=row.get("transactionID", ""),
            ))
        return out
