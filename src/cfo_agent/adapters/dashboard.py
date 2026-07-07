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
from collections import defaultdict

from flask import Flask, Response, g, jsonify, request

from ..config import RUNS_LOCAL, load_client
from ..engine import kv, ledger, receipts
from ..engine.dm_assemble import BILLABLE_CANDIDATE, _merchant, _money

app = Flask(__name__)
_HAVE_RECEIPT = ("stored", "referenced", "received")


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
    """Active close month: the DB value (admin DM) wins, CLOSE_MONTH env is
    the fallback."""
    return kv.active_month(_client(), os.environ.get("CLOSE_MONTH"))


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
    """Reviewer sign-off: approve every categorized line for the active month
    (they become next month's top precedent). Uncategorized lines stay open."""
    month = _month()
    if not month:
        return jsonify(ok=False, error="no close month set"), 500
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
            f'<td>{html.escape(l.get("project") or "")}</td>'
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
    month = _month()
    if not month:
        return Response("No close month set — set CLOSE_MONTH, or have an admin "
                        'DM Penny "start the 2026-07 close".', 500)
    cfg = load_client(client)
    coa = cfg.coa_lines
    conn = _db()
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

    return PAGE.format(month=month, n=len(lines), total=_money(total),
                       corrections=corrections, done=done, npals=len(by_pal),
                       body="".join(sections), gen=ledger.now())


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
<div class="meta"><b>{n}</b> charges · <b>{total}</b> · <b>{corrections}</b> reviewer corrections ·
<b>{done}/{npals}</b> people fully done · <span id="status"></span></div></header>
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
  if(!confirm('Approve all categorized {month} charges? Approved lines become '
              +'next month\\'s precedent. Uncategorized lines stay open.'))return;
  fetch('/approve',{{method:'POST'}}).then(function(r){{return r.json();}}).then(function(j){{
    alert(j.ok?('Approved '+j.approved+' line(s)'
          +(j.blocked?('; '+j.blocked+' still need a category'):'')):('Failed: '+j.error));
    location.reload();
  }}).catch(function(){{alert('Approve failed — try again.');}});
}};
</script>
</body></html>"""
