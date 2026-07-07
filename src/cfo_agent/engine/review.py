"""A point-in-time xlsx snapshot of a close, written to the Drive runs folder.

NOTE: since the Railway migration this is a read-only EXPORT for records — the
live, editable review surface is the Penny dashboard (edits there write straight
to Postgres). The old edit-the-dropdown-and-re-import loop is retired. The COA
column still renders as a dropdown, but changes in the file are not read back;
make corrections in the dashboard.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

FILLS = {
    "high": PatternFill("solid", start_color="C6EFCE"),
    "medium": PatternFill("solid", start_color="FFEB9C"),
    "low": PatternFill("solid", start_color="FFC7CE"),
}
HEADER_FONT = Font(bold=True)

# (header, line-key, width). Proposed COA is the editable decision column.
LEDGER_COLS = [
    ("Date", "txn_date", 11), ("Merchant", "merchant_raw", 42),
    ("Amount", None, 11), ("Cardholder", "cardholder", 18),
    ("Employee", "employee", 24), ("Proposed COA", "proposed_coa_line", 30),
    ("Conf", "confidence", 8), ("By", "proposed_by", 8),
    # Cross-check only, never a proposal source — goes away when employees
    # stop coding in Expensify, and nothing else changes.
    ("Expensify (today)", "truth_category", 26),
    ("Billable", None, 9), ("Receipt", None, 9),
    ("Status", "status", 9), ("Rationale", "rationale", 50),
    ("ID", "external_id", 26),   # hidden — re-import key
]
COA_COL = next(i for i, c in enumerate(LEDGER_COLS, 1) if c[0] == "Proposed COA")
BILLABLE_COL = next(i for i, c in enumerate(LEDGER_COLS, 1) if c[0] == "Billable")
ID_COL = len(LEDGER_COLS)


def write_review_workbook(lines: list, gaps: dict, summary: dict, dest: Path,
                          coa_lines: list = None) -> Path:
    wb = Workbook()

    ws = wb.active
    ws.title = "Summary"
    ws.append(["Expense Close — agent draft (nothing posts without approval)"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Read-only snapshot for records. To make corrections, use the live "
               "Penny dashboard (penny-dashboard-production.up.railway.app) — edits "
               "there save to the ledger instantly and teach next month's proposals."])
    ws.append([])
    for k, v in summary.items():
        ws.append([k, v])
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 60

    # Hidden sheet holding the COA list, so the dropdown isn't capped at 255 chars.
    coa_lines = coa_lines or []
    lists = wb.create_sheet("_lists")
    for i, name in enumerate(coa_lines, 1):
        lists.cell(row=i, column=1, value=name)
    lists.sheet_state = "hidden"

    ws = wb.create_sheet("Ledger")
    _sheet_of_lines(ws, [l for l in lines if l["status"] != "excluded"
                         and (l["source"] == "card_feed" or l.get("reimbursable"))],
                    coa_count=len(coa_lines))

    ws = wb.create_sheet("Gaps")
    ws.append(["Gap type", "Date", "Merchant / detail", "Amount", "Who"])
    for c in ws[1]:
        c.font = HEADER_FONT
    for l in gaps.get("card_without_receipt", []):
        ws.append(["card charge, no receipt found", l["txn_date"], l["merchant_raw"],
                   l["amount_cents"] / 100, l.get("cardholder") or ""])
    for l in gaps.get("vault_without_charge", []):
        ws.append(["receipt/expense, no card charge", l["txn_date"], l["merchant_raw"],
                   l["amount_cents"] / 100, l.get("employee") or ""])
    for d, cents in gaps.get("rec_report_gaps", []):
        ws.append(["statement charge missing from books rec", d, "", cents / 100, ""])
    awaiting = gaps.get("rec_awaiting", [])
    if awaiting:
        ws.append(["awaiting next reconciliation (self-resolves — see note)",
                   f"{awaiting[0][0]} … {awaiting[-1][0]}",
                   f"{len(awaiting)} charges not yet reconciled in QuickBooks",
                   sum(c for _, c in awaiting) / 100, ""])
    ws.append([])
    ws.append(["note", "", gaps.get("rec_report_note", ""), "", ""])
    for col, w in zip("ABCDE", (34, 11, 46, 11, 22)):
        ws.column_dimensions[col].width = w

    ws = wb.create_sheet("Excluded")
    _sheet_of_lines(ws, [l for l in lines if l["status"] == "excluded"], coa_count=0)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        wb.save(tmp.name)
        shutil.move(tmp.name, dest)
    return dest


def _sheet_of_lines(ws, lines, coa_count=0):
    ws.append([c[0] for c in LEDGER_COLS])
    for c in ws[1]:
        c.font = HEADER_FONT
    for l in lines:
        row = []
        for title, key, _ in LEDGER_COLS:
            if title == "Amount":
                row.append(l["amount_cents"] / 100)
            elif title == "Billable":
                row.append("YES" if l.get("billable") else "")
            elif title == "Receipt":
                row.append({"stored": "stored", "referenced": "ref-only (not filed)"}
                           .get(l.get("receipt_status"), "MISSING"))
            elif title == "Proposed COA":
                row.append(l.get("proposed_coa_line") or "(reviewer to choose)")
            else:
                row.append(l.get(key) or "")
        ws.append(row)
        conf = l.get("confidence")
        if conf in FILLS:
            ws.cell(row=ws.max_row, column=COA_COL).fill = FILLS[conf]
    for i, (_, _, w) in enumerate(LEDGER_COLS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.column_dimensions[get_column_letter(ID_COL)].hidden = True
    ws.freeze_panes = "A2"

    last = ws.max_row
    if coa_count and last >= 2:
        coa_ref = f"_lists!$A$1:$A${coa_count}"
        dv = DataValidation(type="list", formula1=coa_ref, allow_blank=True)
        dv.error = "Pick a category from the list."
        dv.prompt = "Choose the correct chart-of-accounts category."
        ws.add_data_validation(dv)
        col = get_column_letter(COA_COL)
        dv.add(f"{col}2:{col}{last}")
        bdv = DataValidation(type="list", formula1='"YES,NO"', allow_blank=True)
        ws.add_data_validation(bdv)
        bcol = get_column_letter(BILLABLE_COL)
        bdv.add(f"{bcol}2:{bcol}{last}")
