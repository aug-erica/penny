"""Write Penny's categorizations back to QuickBooks.

Each posted card charge sits in 'Credit Card Pending' (the bank-feed side).
Penny flips the expense line to the mapped GL account (and marks it billable to a
customer where applicable) via a sparse Purchase update. Nothing is created — we
only complete the second side of entries that already exist, so no duplicates.
"""
from __future__ import annotations

import calendar
from datetime import date

import httpx
import yaml
from rapidfuzz import fuzz

from ...config import CLIENTS_DIR
from ...engine.normalize import normalize_merchant
from ..card_feed.qbo_feed import MINOR_VERSION

PENDING_ID = "1150040010"

# How close a project name must match a QBO customer to auto-assign it. Biased
# high on purpose: billing the WRONG client (bad invoice) is worse than leaving
# it unresolved (Penny just asks). Below this, we ask rather than guess.
CUSTOMER_MATCH_MIN = 90


def load_mapping(client: str) -> dict:
    d = yaml.safe_load((CLIENTS_DIR / client / "qbo_accounts.yaml").read_text())
    return {k: v["qbo_id"] for k, v in d["expense_accounts"].items()}


# ---- billable customer resolution -----------------------------------------
def all_customers(q) -> list:
    """Every active QBO customer/sub-customer (paginated — the API caps a page at
    100, and August has >100, so the old un-paginated query silently missed
    clients past the first page)."""
    out, start = [], 1
    while True:
        b = q.query(f"SELECT Id, DisplayName, FullyQualifiedName FROM Customer "
                    f"STARTPOSITION {start} MAXRESULTS 100")
        if not b:
            break
        out += b
        start += len(b)
        if len(b) < 100:
            break
    return out


def customer_candidates(q, project: str, n: int = 3, customers=None) -> list:
    """Top-n [(id, display_name, score)] fuzzy matches for a project name — used
    to offer the pal choices when we can't confidently auto-assign."""
    if not project:
        return []
    custs = customers if customers is not None else all_customers(q)
    pl = project.lower().strip()
    scored = [(c["Id"], c.get("DisplayName") or "",
               fuzz.token_set_ratio(pl, (c.get("DisplayName") or "").lower()))
              for c in custs]
    scored.sort(key=lambda t: t[2], reverse=True)
    return scored[:n]


def resolve_customer(q, project: str, customers=None):
    """Project name -> QBO customer id, or None if no confident match. QBO
    'customers' ARE the engagements (e.g. 'ACLU Privacy Governance'), so a pal's
    project usually maps 1:1 — but names drift, so: exact, then substring, then a
    strict fuzzy threshold; anything softer we leave for Penny to ask about."""
    if not project:
        return None
    custs = customers if customers is not None else all_customers(q)
    pl = project.lower().strip()
    for c in custs:                                   # exact
        if (c.get("DisplayName") or "").lower() == pl:
            return c["Id"]
    for c in custs:                                   # containment either way
        dn = (c.get("DisplayName") or "").lower()
        if dn and (dn in pl or pl in dn):
            return c["Id"]
    cands = customer_candidates(q, project, 1, customers=custs)  # fuzzy
    if cands and cands[0][2] >= CUSTOMER_MATCH_MIN:
        return cands[0][0]
    return None


# ---- receipt attachment ----------------------------------------------------
_MAGIC = [
    (b"%PDF", "application/pdf", ".pdf"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]


def _sniff(path) -> tuple:
    """(content_type, extension) from magic bytes; default to PDF."""
    with open(path, "rb") as f:
        head = f.read(16)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heif", b"mif1"):
        return "image/heic", ".heic"
    for sig, ct, ext in _MAGIC:
        if head.startswith(sig):
            return ct, ext
    return "application/pdf", ".pdf"


def attach_receipt(q, purchase_id: str, file_path, filename: str = "") -> str:
    """Attach a receipt file to a QBO Purchase via the /upload (Attachable)
    endpoint, so it shows right on the expense — no manual matching. Returns the
    new Attachable Id."""
    import json as _json

    import re as _re

    ct, ext = _sniff(file_path)
    # Sanitize: QBO rejects filenames with '/', '*', etc. Merchant descriptors
    # (e.g. "APPLE.COM/US", "UBER *BUSINESS") carry them, so slug to safe chars
    # and always end with the sniffed extension.
    base = _re.sub(r"[^A-Za-z0-9._-]+", "-", filename or "").strip("-.")
    base = (base or f"receipt-{purchase_id}")[:80]
    filename = base if base.lower().endswith(ext) else base + ext
    metadata = {
        "AttachableRef": [{"EntityRef": {"type": "Purchase", "value": str(purchase_id)},
                           "IncludeOnSend": False}],
        "FileName": filename,
        "ContentType": ct,
    }
    if not q._access_token:
        q._refresh_access_token()
    with open(file_path, "rb") as fh:
        content = fh.read()
    # Multipart: a JSON metadata part + the file content part, paired by the _01
    # suffix. Metadata part carries no filename (that's how QBO tells them apart).
    files = [
        ("file_metadata_01", (None, _json.dumps(metadata), "application/json")),
        ("file_content_01", (filename, content, ct)),
    ]
    url = f"{q.base}/v3/company/{q.realm_id}/upload?minorversion={MINOR_VERSION}"
    resp = httpx.post(url, headers={"Authorization": f"Bearer {q._access_token}",
                                    "Accept": "application/json"},
                      files=files, timeout=120)
    if resp.status_code == 401:                        # token aged out mid-run
        q._refresh_access_token()
        resp = httpx.post(url, headers={"Authorization": f"Bearer {q._access_token}",
                                        "Accept": "application/json"},
                          files=files, timeout=120)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"QBO attach {purchase_id} failed: "
                           f"HTTP {resp.status_code} {resp.text[:300]}")
    # The /upload endpoint returns HTTP 200 even when a file part fails — the
    # per-file result carries a Fault. Surface it so failures aren't silent.
    ar = (resp.json().get("AttachableResponse") or [{}])[0]
    if ar.get("Fault"):
        raise RuntimeError(f"QBO attach {purchase_id} fault: {ar['Fault']}")
    att = ar.get("Attachable") or {}
    aid = att.get("Id")
    if not aid and att.get("FileAccessUri"):        # id trails the download URI
        aid = att["FileAccessUri"].rstrip("/").split("/")[-1]
    return aid


def fetch_pending(q, month: str = "2026-06") -> list:
    """Full Purchase objects (need SyncToken + Line) that have a line still coded
    to Credit Card Pending, for the given close month (YYYY-MM)."""
    y, m = (int(x) for x in month.split("-"))
    m_start, m_end = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    out, start = [], 1
    while True:
        b = q.query(f"SELECT * FROM Purchase WHERE TxnDate >= '{m_start}' AND "
                    f"TxnDate <= '{m_end}' STARTPOSITION {start} MAXRESULTS 100")
        if not b:
            break
        for p in b:
            if any((ln.get("AccountBasedExpenseLineDetail") or {}).get("AccountRef", {})
                   .get("value") == PENDING_ID for ln in p.get("Line", [])):
                out.append(p)
        start += len(b)
        if len(b) < 100:
            break
    return out


def plan(q, cfg, penny_lines: list, mapping: dict, cardholder_of,
         transcarent_id=None, month: str = "2026-06") -> dict:
    """Match each pending charge to a Penny category → build write-ops.
    Returns {ops, unmatched, unmapped}."""
    charges = fetch_pending(q, month)
    pen = [l for l in penny_lines if l.get("proposed_coa_line")
           and l["status"] != "excluded" and l["amount_cents"] > 0]
    for e in pen:
        e["_d"] = date.fromisoformat(e["txn_date"])
    used, ops, unmatched, unmapped = set(), [], [], []
    # pending line info per purchase
    def pending_line(p):
        for ln in p["Line"]:
            if (ln.get("AccountBasedExpenseLineDetail") or {}).get("AccountRef", {}).get("value") == PENDING_ID:
                return ln
    def match(x, who):
        xd = date.fromisoformat(x["date"]); xm = normalize_merchant(x["merch"])
        ex = [p for p in pen if id(p) not in used and p["amount_cents"] == x["cents"]
              and abs((p["_d"] - xd).days) <= 3]
        if ex:
            ex.sort(key=lambda p: fuzz.partial_ratio(xm, p["merchant_norm"]), reverse=True)
            return ex[0]
        tol = [p for p in pen if id(p) not in used and p.get("cardholder") == who
               and abs((p["_d"] - xd).days) <= 4 and abs(p["amount_cents"] - x["cents"]) <= 500
               and fuzz.partial_ratio(xm, p["merchant_norm"]) >= 75]
        return max(tol, key=lambda p: fuzz.partial_ratio(xm, p["merchant_norm"]), default=None)
    rows = []
    for p in charges:
        ln = pending_line(p)
        who = cardholder_of(p.get("AccountRef", {}).get("name", ""))
        rows.append((p, {"date": p["TxnDate"], "cents": round(float(ln["Amount"]) * 100),
                         "merch": (ln.get("Description") or "").strip(), "who": who}))
    for p, x in sorted(rows, key=lambda r: -r[1]["cents"]):
        m = match(x, x["who"])
        if not m:
            unmatched.append(x); continue
        used.add(id(m))
        gl = mapping.get(m["proposed_coa_line"])
        if not gl:
            unmapped.append({**x, "coa": m["proposed_coa_line"]}); continue
        bill = (transcarent_id if m.get("billable") == 1
                and (m.get("project") or "").startswith("Transcarent") else None)
        ops.append({"purchase": p, "gl_id": gl, "coa": m["proposed_coa_line"],
                    "merch": x["merch"], "cents": x["cents"], "who": x["who"],
                    "date": x["date"], "billable_customer": bill})
    return {"ops": ops, "unmatched": unmatched, "unmapped": unmapped, "n_charges": len(charges)}


# ---- reimbursement Bills --------------------------------------------------
def resolve_vendor_by_name(q, display_name: str):
    """QBO Vendor id for an exact DisplayName (case-insensitive). No fuzzy match —
    a wrong vendor books the reimbursement to the wrong person; the employee->vendor
    map is explicit (Natalie's list) precisely to avoid guessing."""
    if not display_name:
        return None
    safe = display_name.replace("'", "\\'")
    rows = q.query(f"SELECT Id, DisplayName FROM Vendor WHERE DisplayName = '{safe}'")
    for r in rows:
        if (r.get("DisplayName") or "").lower() == display_name.lower():
            return r["Id"]
    return rows[0]["Id"] if rows else None


def bill_body(vendor_id, gl_id, amount_cents, txn_date, memo=None,
              ap_account_id=None, line_desc=None) -> dict:
    """The QBO Bill request body — built separately so a dry-run can show exactly
    what would post without hitting QBO."""
    line = {"DetailType": "AccountBasedExpenseLineDetail",
            "Amount": round(amount_cents / 100.0, 2),
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": str(gl_id)}}}
    desc = (line_desc or memo or "").strip()
    if desc:
        line["Description"] = desc[:1000]
    body = {"VendorRef": {"value": str(vendor_id)}, "TxnDate": txn_date, "Line": [line]}
    if memo:
        body["PrivateNote"] = memo
    if ap_account_id:                      # which liability the Bill credits (else default A/P)
        body["APAccountRef"] = {"value": str(ap_account_id)}
    return body


def create_bill(q, vendor_id, gl_id, amount_cents, txn_date, memo=None,
                ap_account_id=None, line_desc=None) -> dict:
    """Create a QBO Bill: DR the expense GL, CR the payable account (ap_account_id,
    or the company default A/P if omitted). Used for employee reimbursements
    (vendor = employee). Returns the created Bill object."""
    body = bill_body(vendor_id, gl_id, amount_cents, txn_date, memo,
                     ap_account_id, line_desc)
    if not q._access_token:
        q._refresh_access_token()
    url = f"{q.base}/v3/company/{q.realm_id}/bill?minorversion={MINOR_VERSION}"
    resp = httpx.post(url, headers={"Authorization": f"Bearer {q._access_token}",
                                    "Accept": "application/json",
                                    "Content-Type": "application/json"},
                      json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"QBO create Bill failed: HTTP {resp.status_code} "
                           f"{resp.text[:400]}")
    return resp.json().get("Bill", resp.json())


def bill_body_lines(vendor_id, lines, txn_date, memo=None, ap_account_id=None) -> dict:
    """A multi-line Bill body. `lines` = [{gl_id, amount_cents, description}] — one
    expense line per reimbursement, each on its own category GL. Used to group an
    employee's month of reimbursements into a single Bill."""
    body_lines = []
    for l in lines:
        bl = {"DetailType": "AccountBasedExpenseLineDetail",
              "Amount": round(l["amount_cents"] / 100.0, 2),
              "AccountBasedExpenseLineDetail": {"AccountRef": {"value": str(l["gl_id"])}}}
        desc = (l.get("description") or "").strip()
        if desc:
            bl["Description"] = desc[:1000]
        body_lines.append(bl)
    body = {"VendorRef": {"value": str(vendor_id)}, "TxnDate": txn_date, "Line": body_lines}
    if memo:
        body["PrivateNote"] = memo
    if ap_account_id:
        body["APAccountRef"] = {"value": str(ap_account_id)}
    return body


def create_bill_lines(q, vendor_id, lines, txn_date, memo=None, ap_account_id=None) -> dict:
    """Create a multi-line Bill (grouped reimbursements). Returns the Bill object."""
    body = bill_body_lines(vendor_id, lines, txn_date, memo, ap_account_id)
    if not q._access_token:
        q._refresh_access_token()
    url = f"{q.base}/v3/company/{q.realm_id}/bill?minorversion={MINOR_VERSION}"
    resp = httpx.post(url, headers={"Authorization": f"Bearer {q._access_token}",
                                    "Accept": "application/json",
                                    "Content-Type": "application/json"},
                      json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"QBO create Bill failed: HTTP {resp.status_code} "
                           f"{resp.text[:400]}")
    return resp.json().get("Bill", resp.json())


def billpayment_body(bill_id, vendor_id, amount_cents, txn_date,
                     credit_account_id, ap_account_id=None) -> dict:
    """A BillPayment that pays `bill_id` with the offset going to
    `credit_account_id` (the 1345 reimbursement clearing account). Natalie set 1345
    as a *Credit Card*-type account (Justworks can't post to a Bank-type), so the
    payment funds from it via PayType=CreditCard / CreditCardPayment.CCAccountRef."""
    amt = round(amount_cents / 100.0, 2)
    body = {"VendorRef": {"value": str(vendor_id)}, "TotalAmt": amt, "TxnDate": txn_date,
            "PayType": "CreditCard",
            "CreditCardPayment": {"CCAccountRef": {"value": str(credit_account_id)}},
            "Line": [{"Amount": amt,
                      "LinkedTxn": [{"TxnId": str(bill_id), "TxnType": "Bill"}]}]}
    if ap_account_id:
        body["APAccountRef"] = {"value": str(ap_account_id)}
    return body


def create_bill_payment(q, bill_id, vendor_id, amount_cents, txn_date,
                        credit_account_id, ap_account_id=None) -> dict:
    """Pay a Bill, crediting `credit_account_id` (the reimbursement clearing acct)."""
    body = billpayment_body(bill_id, vendor_id, amount_cents, txn_date,
                            credit_account_id, ap_account_id)
    if not q._access_token:
        q._refresh_access_token()
    url = f"{q.base}/v3/company/{q.realm_id}/billpayment?minorversion={MINOR_VERSION}"
    resp = httpx.post(url, headers={"Authorization": f"Bearer {q._access_token}",
                                    "Accept": "application/json",
                                    "Content-Type": "application/json"},
                      json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"QBO create BillPayment failed: HTTP {resp.status_code} "
                           f"{resp.text[:400]}")
    return resp.json().get("BillPayment", resp.json())


def commit_one(q, op) -> dict:
    """Sparse Purchase update: flip the pending line to the mapped GL (QBO replaces
    the whole Line array on update, so we resend all lines with the one changed)."""
    p = op["purchase"]
    new_lines = []
    for ln in p["Line"]:
        d = ln.get("AccountBasedExpenseLineDetail")
        # Set the expense line to the target GL. Works both for the initial flip
        # (line currently in Credit Card Pending) and a later re-categorization
        # (line already at some category) — these card charges are single-line.
        if d:
            nd = dict(d)
            if op.get("gl_id"):                       # keep the existing category if omitted
                nd["AccountRef"] = {"value": op["gl_id"]}
            if op.get("billable_customer"):
                nd["BillableStatus"] = "Billable"
                nd["CustomerRef"] = {"value": op["billable_customer"]}
            nl = dict(ln); nl["AccountBasedExpenseLineDetail"] = nd
            if op.get("note"):                        # invoice description for billables
                base = (ln.get("Description") or "").strip()
                nl["Description"] = f"{base}: {op['note']}" if base else op["note"]
            new_lines.append(nl)
        else:
            new_lines.append(ln)
    # Sparse update still requires Purchase's required fields — echo them back.
    body = {"sparse": True, "Id": p["Id"], "SyncToken": p["SyncToken"], "Line": new_lines,
            "PaymentType": p["PaymentType"], "AccountRef": p["AccountRef"]}
    if not q._access_token:
        q._refresh_access_token()
    url = f"{q.base}/v3/company/{q.realm_id}/purchase?minorversion={MINOR_VERSION}"
    resp = httpx.post(url, headers={"Authorization": f"Bearer {q._access_token}",
                                    "Accept": "application/json",
                                    "Content-Type": "application/json"}, json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"QBO update {p['Id']} failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json()
