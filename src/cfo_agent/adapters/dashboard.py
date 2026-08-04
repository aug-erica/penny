"""Live, editable review dashboard for the current close month.

Reads the ledger straight from Postgres (DATABASE_URL) on every request, so it is
always current. Reviewers change a charge's category or billable flag inline and
it writes back to Postgres immediately — replacing the download-edit-reimport
workbook loop. Deployed as its own Railway service (ROLE=dashboard), served by
gunicorn, gated by HTTP basic auth (DASHBOARD_PASSWORD; any username).
"""
from __future__ import annotations

import html
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, request

from ..config import RUNS_LOCAL, load_client
from ..engine import kv, ledger, receipts, reimburse_flow, reimburse_policy
from ..engine.dm_assemble import BILLABLE_CANDIDATE, _merchant, _money

app = Flask(__name__)
_HAVE_RECEIPT = ("stored", "referenced", "received")
# Order the reimbursement queue so items needing action float to the top.
_REIMB_ORDER = {"needs_info": 0, "submitted": 1, "approved": 2,
                "exported": 3, "paid": 4, "rejected": 5}


def _maybe_slack():
    """A PennySlack if the bot token is present on this service, else None. The
    dashboard service may not carry a Slack token, so employee DMs are best-effort
    (approval/rejection still succeed without them)."""
    try:
        from .slack_client import PennySlack
        return PennySlack()
    except Exception:
        return None


def _client():
    return os.environ.get("DASH_CLIENT", "august")


def _db():
    # One connection per request, closed on teardown — avoids leaking DB
    # connections (which would exhaust Postgres and hang the service).
    if "db" not in g:
        g.db = ledger.open_db(RUNS_LOCAL / _client() / "ledger.sqlite3")  # DATABASE_URL→PG
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _month():
    """Active close month: an admin's DB override (Slack "start the … close") wins,
    else the CURRENT calendar month — matching the listener's active_month(), so the
    dashboard tracks the rolling close. (Previously it fell back to a static
    CLOSE_MONTH env, which left the dashboard showing June while charges and
    reimbursements were posting to July.)"""
    import datetime as _dt
    return kv.active_month(_client(),
                           _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m"))


def _view_month():
    """The month being VIEWED — a ?month=YYYY-MM query param (the dropdown) if valid,
    else the active month. This is per-request navigation only; it never changes the
    bot's active month (no kv write)."""
    q = request.args.get("month")
    if q and re.fullmatch(r"\d{4}-\d{2}", q):
        return q
    return _month()


def _months_available(conn, client: str) -> list:
    """Distinct close months present in the data (card lines + reimbursements),
    newest first, always including the active month — the dropdown's options."""
    months = set()
    for t in ("ledger_lines", "reimbursements"):
        try:
            for r in conn.execute(f"SELECT DISTINCT close_month FROM {t} WHERE client=?",
                                  (client,)):
                if r["close_month"]:
                    months.add(r["close_month"])
        except Exception:
            pass
    months.add(_month())
    return sorted(months, reverse=True)


def _month_selector(available: list, current: str, base: str) -> str:
    opts = "".join(f'<option value="{m}"{" selected" if m == current else ""}>{m}</option>'
                   for m in available)
    return (f'<select id="monthsel" class="monthsel" '
            f'onchange="location=\'{base}?month=\'+this.value">{opts}</select>')


@app.before_request
def _guard():
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not pw:
        # Fail CLOSED: this is company card data on a public URL — an unset
        # password must never mean an open dashboard.
        return Response("Dashboard not configured — set DASHBOARD_PASSWORD.", 503)
    auth = request.authorization
    if not (auth and auth.password == pw):
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="Penny dashboard"'})


@app.route("/update", methods=["POST"])
def update():
    d = request.get_json(force=True, silent=True) or {}
    try:
        lid = int(d["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="bad id"), 400
    field, val = d.get("field"), d.get("value")
    conn = _db()
    row = conn.execute("SELECT external_id, close_month, merchant_norm, project "
                       "FROM ledger_lines WHERE id=?", (lid,)).fetchone()
    if not row:
        return jsonify(ok=False, error="no such line"), 404
    if field == "coa" and val:
        ledger.set_proposal(conn, lid, val, "reviewer", "high",
                            "reviewer edit (dashboard)")
        # Flywheel: this correction becomes a durable rule for next month.
        ledger.upsert_learned_rule(conn, _client(), row["merchant_norm"], val, "reviewer")
        ledger.record_decision(conn, _client(), row["close_month"], row["external_id"],
                               "category",
                               {"coa_line": val, "rationale": "reviewer edit (dashboard)"},
                               decided_by="dashboard", source="dashboard")
    elif field == "billable":
        b = 1 if val == "yes" else (0 if val == "no" else None)
        proj = row["project"] if b else None
        ledger.set_billable_project(conn, lid, b, proj)
        ledger.record_decision(conn, _client(), row["close_month"], row["external_id"],
                               "billable_project", {"billable": b, "project": proj},
                               decided_by="dashboard", source="dashboard")
    else:
        return jsonify(ok=False, error="bad field"), 400
    return jsonify(ok=True)


@app.route("/approve", methods=["POST"])
def approve():
    """Reviewer sign-off: approve every categorized line for the month being viewed
    (they become next month's top precedent). Uncategorized lines stay open."""
    month = (request.get_json(force=True, silent=True) or {}).get("month") or _month()
    res = ledger.approve_month(_db(), _client(), month,
                               decided_by="dashboard", force=True)
    return jsonify(ok=True, approved=res["approved"], already=res["already"],
                   blocked=len(res["blocked"]))


def _coa_select(l: dict, coa: list) -> str:
    cur = l.get("proposed_coa_line")
    blank = '<option value=""%s>—</option>' % ("" if cur else " selected")
    opts = "".join(
        f'<option{" selected" if c == cur else ""}>{html.escape(c)}</option>' for c in coa)
    return (f'<select class="edit" data-id="{l["id"]}" data-field="coa">'
            f'{blank}{opts}</select>')


def _bill_select(l: dict) -> str:
    cur = l.get("billable")
    v = "yes" if cur == 1 else ("no" if cur == 0 else "")
    o = [("", "—"), ("yes", "YES"), ("no", "no")]
    opts = "".join(f'<option value="{val}"{" selected" if val == v else ""}>{lab}</option>'
                   for val, lab in o)
    return (f'<select class="edit bill" data-id="{l["id"]}" data-field="billable">'
            f'{opts}</select>')


def _row(l: dict, needed_ids: set, coa: list) -> str:
    rs = l.get("receipt_status")
    receipt = "✓" if rs in _HAVE_RECEIPT else ("needed" if l["id"] in needed_ids else "—")
    cls = []
    if not l.get("proposed_coa_line"):
        cls.append("uncat")
    if l["id"] in needed_ids and rs not in _HAVE_RECEIPT:
        cls.append("missing")
    if l.get("proposed_by") == "reviewer":
        cls.append("reviewed")
    conf = l.get("confidence") or ""
    return (f'<tr class="{" ".join(cls)}">'
            f'<td>{l["txn_date"][5:]}</td>'
            f'<td class="m">{html.escape(_merchant(l["merchant_raw"]))}</td>'
            f'<td class="r">{_money(l["amount_cents"])}</td>'
            f'<td>{_coa_select(l, coa)}</td>'
            f'<td class="c">{_bill_select(l)}</td>'
            f'<td>{html.escape(l.get("project") or "")}'
            + (f'<br><span class="note">📝 {html.escape(l["billable_note"])}</span>'
               if l.get("billable_note") else "")
            + '</td>'
            f'<td class="c rc-{receipt}">{receipt}</td>'
            f'<td class="c cf-{conf}">{conf}</td>'
            f'<td class="c by">{html.escape(l.get("proposed_by") or "")}</td>'
            f'</tr>')


def _pal_open(charges: list) -> int:
    untagged = [c for c in charges if c.get("proposed_coa_line") in BILLABLE_CANDIDATE
                and not c.get("project") and c.get("billable") != 0]
    needed = receipts.receipt_needed(charges)
    missing = [c for c in needed if c.get("receipt_status") not in _HAVE_RECEIPT]
    uncat = [c for c in charges if not c.get("proposed_coa_line")]
    return len(untagged) + len(missing) + len(uncat)


@app.route("/")
def index():
    client = _client()
    month = _view_month()
    cfg = load_client(client)
    coa = cfg.coa_lines
    conn = _db()
    monthsel = _month_selector(_months_available(conn, client), month, "/")
    lines = [l for l in ledger.lines_for_month(conn, client, month)
             if l["status"] != "excluded" and l["amount_cents"] > 0]
    by_pal = defaultdict(list)
    for l in lines:
        by_pal[l.get("cardholder") or "(unknown)"].append(l)

    total = sum(l["amount_cents"] for l in lines)
    corrections = sum(1 for l in lines if l.get("proposed_by") == "reviewer")
    done = sum(1 for ch in by_pal.values() if _pal_open(ch) == 0)

    sections = []
    for pal in sorted(by_pal):
        ch = sorted(by_pal[pal], key=lambda l: l["txn_date"])
        needed_ids = {c["id"] for c in receipts.receipt_needed(ch)}
        openn = _pal_open(ch)
        sub = sum(l["amount_cents"] for l in ch)
        chip = ('<span class="chip ok">all set</span>' if openn == 0 else
                f'<span class="chip open">{openn} open</span>')
        rows = "".join(_row(l, needed_ids, coa) for l in ch)
        sections.append(
            f'<section><h2>{html.escape(pal)} <span class="sub">{len(ch)} charges · '
            f'{_money(sub)}</span> {chip}</h2>'
            '<table><thead><tr><th>Date</th><th>Merchant</th><th class="r">Amount</th>'
            '<th>Category</th><th>Bill</th><th>Project</th><th>Receipt</th>'
            '<th>Conf</th><th>By</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></section>')

    return PAGE.format(month=month, monthsel=monthsel, n=len(lines), total=_money(total),
                       corrections=corrections, done=done, npals=len(by_pal),
                       body="".join(sections), gen=ledger.now())


# --- Reimbursements queue --------------------------------------------------

def _reimb_coa_select(r: dict, coa: list) -> str:
    cur = r.get("proposed_coa_line")
    editable = r["status"] in ("submitted", "needs_info")
    if not editable:
        return html.escape(cur or "—")
    opts = "".join(
        f'<option{" selected" if c == cur else ""}>{html.escape(c)}</option>' for c in coa)
    blank = '<option value=""%s>—</option>' % ("" if cur else " selected")
    return (f'<select class="redit" data-id="{r["id"]}" data-field="coa">'
            f'{blank}{opts}</select>')


def _reimb_row(r: dict, coa: list, violations: list = None) -> str:
    st = r["status"]
    receipt = "✓" if r.get("receipt_status") in _HAVE_RECEIPT else (
        "n/a" if r.get("receipt_status") == "not_required" else "needed")
    flag = ""
    if violations:
        tip = " · ".join(v["message"] for v in violations)
        flag = f'<br><span class="polflag" title="{html.escape(tip)}">⚠ over policy</span>'
    actions = ""
    if st in ("submitted", "needs_info"):
        can_approve = st == "submitted"
        appr = (f'<button class="rapprove" data-id="{r["id"]}">Approve</button>'
                if can_approve else '<span class="mut">needs info</span>')
        actions = (appr + f' <button class="rreject" data-id="{r["id"]}">Reject</button>')
    elif st == "approved":
        actions = '<span class="chip ok">approved</span>'
    elif st == "exported":
        actions = '<span class="chip open">exported</span>'
    elif st == "paid":
        actions = '<span class="chip ok">paid</span>'
    elif st == "rejected":
        actions = f'<span class="chip rej">rejected</span>'
    kind = "🔁" if r.get("kind") == "stipend" else ""
    return (f'<tr class="rst-{st}">'
            f'<td>{html.escape(r.get("employee") or "")} {kind}</td>'
            f'<td>{(r.get("expense_date") or "")[5:]}</td>'
            f'<td class="r">{_money(r["amount_cents"])}</td>'
            f'<td>{html.escape(r.get("business_purpose") or "")}{flag}</td>'
            f'<td>{_reimb_coa_select(r, coa)}</td>'
            f'<td class="c rc-{receipt}">{receipt}</td>'
            f'<td class="c">{actions}</td></tr>')


@app.route("/reimbursements")
def reimbursements_page():
    client = _client()
    month = _view_month()
    cfg = load_client(client)
    coa = cfg.coa_lines
    conn = _db()
    monthsel = _month_selector(_months_available(conn, client), month, "/reimbursements")
    rows = ledger.reimbursements_for(conn, client, month)
    rows.sort(key=lambda r: (_REIMB_ORDER.get(r["status"], 9), r.get("submitted_at") or ""))
    today = ledger.now()[:10]

    def _viol(r):
        return reimburse_policy.check(cfg, r.get("amount_cents"),
                                      r.get("business_purpose"),
                                      r.get("proposed_coa_line"),
                                      r.get("expense_date"), today)
    body = "".join(_reimb_row(r, coa, _viol(r)) for r in rows) or (
        '<tr><td colspan="7" class="mut" style="padding:16px">No reimbursements '
        'this month yet.</td></tr>')
    n_appr = sum(1 for r in rows if r["status"] == "approved")
    n_exp = sum(1 for r in rows if r["status"] == "exported")
    total = sum(r["amount_cents"] for r in rows if r["status"] not in ("rejected",))
    return REIMB_PAGE.format(month=month, monthsel=monthsel, body=body, n=len(rows),
                             total=_money(total), n_appr=n_appr, n_exp=n_exp,
                             gen=ledger.now())


@app.route("/reimbursements/update", methods=["POST"])
def reimbursements_update():
    d = request.get_json(force=True, silent=True) or {}
    try:
        rid = int(d["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="bad id"), 400
    if d.get("field") == "coa" and d.get("value"):
        ledger.set_reimbursement_coa(_db(), rid, d["value"])
        return jsonify(ok=True)
    return jsonify(ok=False, error="bad field"), 400


@app.route("/reimbursements/approve", methods=["POST"])
def reimbursements_approve():
    d = request.get_json(force=True, silent=True) or {}
    try:
        rid = int(d["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="bad id"), 400
    conn = _db()
    cfg = load_client(_client())
    row = ledger.reimbursement_by_id(conn, rid)
    if not row:
        return jsonify(ok=False, error="not found"), 404
    # Server computes the approver (never trusts the client): the primary reviewer,
    # or the secondary when the submitter IS the primary — so no self-approval.
    approver = reimburse_flow.approver_for(cfg, row["employee"])
    try:
        reimburse_flow.approve(conn, cfg, rid, approver, slack=_maybe_slack())
    except PermissionError as ex:
        return jsonify(ok=False, error=str(ex)), 403
    return jsonify(ok=True, approver=approver)


@app.route("/reimbursements/reject", methods=["POST"])
def reimbursements_reject():
    d = request.get_json(force=True, silent=True) or {}
    try:
        rid = int(d["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="bad id"), 400
    conn = _db()
    cfg = load_client(_client())
    reimburse_flow.reject(conn, cfg, rid, d.get("reason") or "rejected",
                          actor="dashboard", slack=_maybe_slack())
    return jsonify(ok=True)


@app.route("/reimbursements/export", methods=["POST"])
def reimbursements_export():
    """Prepare the Justworks payout for all approved reimbursements this month.
    Returns the copy-paste text + artifact link and marks them exported."""
    from .payment import build_rail
    conn = _db()
    cfg = load_client(_client())
    d = request.get_json(force=True, silent=True) or {}
    month = d.get("month") or _month()
    approved = ledger.reimbursements_for(conn, _client(), month, status="approved")
    if not approved:
        return jsonify(ok=False, error="nothing approved to export"), 400
    pay_date = d.get("pay_date")
    res = reimburse_flow.export_payout(conn, cfg, [r["id"] for r in approved],
                                       build_rail(cfg), pay_date=pay_date)
    result = res["result"]
    return jsonify(ok=result.get("status") in ("exported", "paid"),
                   paste_text=result.get("paste_text", ""),
                   csv_text=result.get("csv_text", ""),
                   ref=result.get("ref"), count=len(res["reimbursements"]))


@app.route("/reimbursements/export.csv", methods=["GET"])
def reimbursements_export_csv():
    """Re-download the Justworks CSV for the month's already-exported rows — so a
    lost download (or a second batch's file) is always recoverable, without
    re-running the export. `pay_date` optional (defaults to today)."""
    from .payment import build_rail
    conn = _db()
    cfg = load_client(_client())
    month = request.args.get("month") or _month()
    pay_date = request.args.get("pay_date") or datetime.now(timezone.utc).strftime("%m/%d/%Y")
    rows = ledger.reimbursements_for(conn, _client(), month, status="exported")
    if not rows:
        return Response(f"Nothing exported for {month} yet.", 404)
    rail = build_rail(cfg)
    csv_text = rail.csv_text(rows, pay_date)
    return Response(csv_text, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="justworks-{month}-exported.csv"'})


@app.route("/reimbursements/mark-paid", methods=["POST"])
def reimbursements_mark_paid():
    conn = _db()
    cfg = load_client(_client())
    month = (request.get_json(force=True, silent=True) or {}).get("month") or _month()
    exported = ledger.reimbursements_for(conn, _client(), month, status="exported")
    paid = reimburse_flow.mark_paid(conn, cfg, [r["id"] for r in exported],
                                    actor="dashboard", slack=_maybe_slack())
    return jsonify(ok=True, count=len(paid))


REIMB_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Penny — reimbursements {month}</title><style>
:root{{--ink:#1a1a1a;--mut:#6b7280;--line:#e5e7eb;--amber:#fef3c7;--grn:#16a34a;}}
*{{box-sizing:border-box}}
body{{font-family:'Work Sans',-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);
margin:0;background:#fafafa;font-size:14px}}
header{{background:#fff;border-bottom:1px solid var(--line);padding:20px 28px}}
h1{{margin:0;font-size:20px;font-weight:600}}
.meta{{color:var(--mut);margin-top:6px;font-size:13px}} .meta b{{color:var(--ink)}}
.monthsel{{font:inherit;font-size:13px;padding:2px 6px;border:1px solid var(--line);border-radius:6px;background:#fff}}
a.nav{{color:#2563eb;text-decoration:none;font-size:13px}}
main{{padding:20px 28px;max-width:1100px;margin:0 auto}}
section{{background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:18px;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid #f1f1f1}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:600}}
td.r,th.r{{text-align:right;font-variant-numeric:tabular-nums}} td.c{{text-align:center}}
.mut{{color:var(--mut)}}
.chip{{font-size:11px;font-weight:600;padding:2px 9px;border-radius:20px}}
.chip.ok{{background:#dcfce7;color:#166534}} .chip.open{{background:#fef3c7;color:#92400e}}
.chip.rej{{background:#fee2e2;color:#991b1b}}
.rc-needed{{color:#92400e;font-weight:600}} .rc-n\\/a{{color:var(--mut)}}
.polflag{{color:#b45309;font-size:11px;font-weight:600;cursor:help}}
tr.rst-needs_info td{{background:#fffbeb}}
button{{font:inherit;font-size:13px;font-weight:600;padding:5px 12px;border-radius:7px;cursor:pointer;border:1px solid var(--line);background:#fff}}
button.rapprove{{border-color:var(--grn);background:#f0fdf4;color:#166534}}
button.rreject{{border-color:#dc2626;background:#fef2f2;color:#991b1b}}
select.redit{{font:inherit;font-size:13px;padding:2px 4px;border:1px solid var(--line);border-radius:5px;max-width:220px}}
#panel{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:18px}}
#paste{{width:100%;height:150px;font-family:ui-monospace,Menlo,monospace;font-size:12px;
border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:10px;display:none}}
#export,#markpaid{{margin-right:8px}}
#export{{border-color:#2563eb;background:#eff6ff;color:#1d4ed8}}
</style></head><body>
<header><h1>Penny · reimbursements</h1>
<div class="meta">Month: {monthsel} · <b>{n}</b> this month · <b>{total}</b> · <b>{n_appr}</b> approved awaiting payout ·
<b>{n_exp}</b> exported · <a class="nav" href="/?month={month}">← card close</a></div></header>
<main>
<div id="panel">
  <button id="export">Prepare Justworks payout ({n_appr} approved) →</button>
  <button id="redownload">⬇︎ Re-download exported CSV ({n_exp})</button>
  <button id="markpaid">Mark exported as paid ({n_exp})</button>
  <span id="pstatus" class="mut"></span>
  <textarea id="paste" readonly placeholder="Copy-paste text for Justworks will appear here"></textarea>
  <div id="artifact" class="mut" style="margin-top:6px"></div>
</div>
<section><table><thead><tr><th>Employee</th><th>Date</th><th class="r">Amount</th>
<th>Purpose</th><th>Category</th><th>Receipt</th><th class="c">Action</th></tr></thead>
<tbody>{body}</tbody></table></section>
</main>
<script>
function post(url,data){{return fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify(data||{{}})}}).then(function(r){{return r.json();}});}}
document.addEventListener('change',function(e){{
  var el=e.target; if(!el.classList.contains('redit'))return;
  post('/reimbursements/update',{{id:el.dataset.id,field:el.dataset.field,value:el.value}});
}});
document.addEventListener('click',function(e){{
  var el=e.target;
  if(el.classList.contains('rapprove')){{
    post('/reimbursements/approve',{{id:el.dataset.id}}).then(function(j){{
      if(j.ok)location.reload(); else alert('Approve failed: '+j.error);}});
  }}
  if(el.classList.contains('rreject')){{
    var reason=prompt('Reason for rejecting?'); if(reason===null)return;
    post('/reimbursements/reject',{{id:el.dataset.id,reason:reason}}).then(function(){{location.reload();}});
  }}
}});
document.getElementById('export').onclick=function(){{
  var month=document.getElementById('monthsel').value;
  var pd=prompt('Justworks pay date? (MM/DD/YYYY — blank = today)'); if(pd===null)return;
  post('/reimbursements/export',{{pay_date:pd||null,month:month}}).then(function(j){{
    if(!j.ok){{document.getElementById('pstatus').textContent=j.error||'export failed';return;}}
    var ta=document.getElementById('paste'); ta.style.display='block'; ta.value=j.paste_text;
    if(j.csv_text){{                       // real browser download — never a lost file
      var blob=new Blob([j.csv_text],{{type:'text/csv'}});
      var url=URL.createObjectURL(blob); var a=document.createElement('a');
      a.href=url; a.download='justworks-'+(j.ref||'export')+'.csv';
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    }}
    document.getElementById('pstatus').textContent='Exported '+j.count+' — CSV downloaded to your computer for the Justworks bulk upload.';
    document.getElementById('artifact').innerHTML='Need it again later? Use “Re-download exported CSV”.';
    ta.select();
  }});
}};
document.getElementById('redownload').onclick=function(){{
  var month=document.getElementById('monthsel').value;
  var pd=prompt('Justworks pay date for the file? (MM/DD/YYYY — blank = today)');
  if(pd===null)return;
  var u='/reimbursements/export.csv?month='+encodeURIComponent(month);
  if(pd)u+='&pay_date='+encodeURIComponent(pd);
  window.location=u;
}};
document.getElementById('markpaid').onclick=function(){{
  var month=document.getElementById('monthsel').value;
  if(!confirm('Mark all exported reimbursements as paid?'))return;
  post('/reimbursements/mark-paid',{{month:month}}).then(function(j){{alert('Marked '+j.count+' paid');location.reload();}});
}};
</script>
</body></html>"""


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Penny — {month} close</title><style>
:root{{--ink:#1a1a1a;--mut:#6b7280;--line:#e5e7eb;--amber:#fef3c7;--red:#fee2e2;--grn:#16a34a;}}
*{{box-sizing:border-box}}
body{{font-family:'Work Sans',-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);
margin:0;background:#fafafa;font-size:14px}}
header{{background:#fff;border-bottom:1px solid var(--line);padding:20px 28px;position:sticky;top:0;z-index:5}}
h1{{margin:0;font-size:20px;font-weight:600}}
.meta{{color:var(--mut);margin-top:6px;font-size:13px}} .meta b{{color:var(--ink)}}
.monthsel{{font:inherit;font-size:13px;padding:2px 6px;border:1px solid var(--line);border-radius:6px;background:#fff}}
main{{padding:20px 28px;max-width:1200px;margin:0 auto}}
section{{background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:18px;overflow:hidden}}
h2{{font-size:15px;font-weight:600;margin:0;padding:14px 16px;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
h2 .sub{{color:var(--mut);font-weight:400;font-size:13px}}
.chip{{font-size:11px;font-weight:600;padding:2px 9px;border-radius:20px}}
.chip.ok{{background:#dcfce7;color:#166534}} .chip.open{{background:#fef3c7;color:#92400e}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:6px 12px;border-bottom:1px solid #f1f1f1;white-space:nowrap}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:600}}
td.r,th.r{{text-align:right;font-variant-numeric:tabular-nums}}
td.c{{text-align:center}} td.m{{max-width:220px;overflow:hidden;text-overflow:ellipsis}}
td.by{{color:var(--mut);font-size:12px}}
.note{{color:var(--mut);font-size:12px;font-style:italic}}
select.edit{{font:inherit;font-size:13px;padding:2px 4px;border:1px solid var(--line);border-radius:5px;
background:#fff;max-width:230px}}
select.bill{{max-width:70px}}
select.saving{{border-color:#f59e0b}} select.saved{{border-color:var(--grn);background:#f0fdf4}}
select.err{{border-color:#dc2626;background:#fef2f2}}
tr.missing td.rc-needed{{background:var(--amber);color:#92400e;font-weight:600}}
tr.uncat td{{background:var(--red)}}
tr.reviewed{{box-shadow:inset 3px 0 0 var(--grn)}}
.cf-low{{color:#b45309}} .cf-high{{color:#166534}}
.foot{{color:var(--mut);font-size:12px;padding:8px 28px 28px;max-width:1200px;margin:0 auto}}
#approve{{float:right;font:inherit;font-size:13px;font-weight:600;padding:6px 14px;
border-radius:8px;border:1px solid var(--grn);background:#f0fdf4;color:#166534;cursor:pointer}}
#approve:hover{{background:#dcfce7}}
</style></head><body>
<header><button id="approve" title="Mark every categorized charge approved — they become next month's precedent">✓ Approve month</button>
<h1>Penny · {month} expense close <span style="color:#6b7280;font-weight:400">— live &amp; editable</span></h1>
<div class="meta">Month: {monthsel} · <b>{n}</b> charges · <b>{total}</b> · <b>{corrections}</b> reviewer corrections ·
<b>{done}/{npals}</b> people fully done · <a href="/reimbursements?month={month}" style="color:#2563eb;text-decoration:none">reimbursements →</a> · <span id="status"></span></div></header>
<main>{body}</main>
<div class="foot">Live from Postgres. Change a Category or Bill dropdown and it saves instantly.
Green bar = reviewer-set · amber = receipt needed · red = uncategorized. (loaded {gen} UTC)</div>
<script>
document.addEventListener('change',function(e){{
  var el=e.target; if(!el.classList.contains('edit'))return;
  el.classList.remove('saved','err'); el.classList.add('saving');
  fetch('/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{id:el.dataset.id,field:el.dataset.field,value:el.value}})}})
  .then(function(r){{return r.json();}}).then(function(j){{
    el.classList.remove('saving'); el.classList.add(j.ok?'saved':'err');
    var row=el.closest('tr'); if(j.ok&&row)row.classList.add('reviewed');
    setTimeout(function(){{el.classList.remove('saved','err');}},1500);
  }}).catch(function(){{el.classList.remove('saving');el.classList.add('err');}});
}});
document.getElementById('approve').onclick=function(){{
  var month=document.getElementById('monthsel').value;
  if(!confirm('Approve all categorized '+month+' charges? Approved lines become '
              +'next month\\'s precedent. Uncategorized lines stay open.'))return;
  fetch('/approve',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{month:month}})}}).then(function(r){{return r.json();}}).then(function(j){{
    alert(j.ok?('Approved '+j.approved+' line(s)'
          +(j.blocked?('; '+j.blocked+' still need a category'):'')):('Failed: '+j.error));
    location.reload();
  }}).catch(function(){{alert('Approve failed — try again.');}});
}};
</script>
</body></html>"""
