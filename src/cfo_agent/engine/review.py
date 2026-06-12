"""The review surface: one xlsx workbook per close, written locally then moved
into the Drive runs folder atomically (Drive sync dislikes in-place writes)."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

FILLS = {
    "high": PatternFill("solid", start_color="C6EFCE"),
    "medium": PatternFill("solid", start_color="FFEB9C"),
    "low": PatternFill("solid", start_color="FFC7CE"),
}
HEADER_FONT = Font(bold=True)

LEDGER_COLS = [
    ("Date", "txn_date", 11), ("Merchant", "merchant_raw", 42),
    ("Amount", None, 11), ("Cardholder", "cardholder", 18),
    ("Employee", "employee", 24), ("Proposed COA", "proposed_coa_line", 26),
    ("Conf", "confidence", 8), ("By", "proposed_by", 8),
    ("Billable", None, 9), ("Receipt", None, 9),
    ("Status", "status", 9), ("Rationale", "rationale", 50),
]


def write_review_workbook(lines: list, gaps: dict, summary: dict, dest: Path) -> Path:
    wb = Workbook()

    ws = wb.active
    ws.title = "Summary"
    ws.append(["Expense Close — agent draft (nothing posts without approval)"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for k, v in summary.items():
        ws.append([k, v])
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 60

    ws = wb.create_sheet("Ledger")
    _sheet_of_lines(ws, [l for l in lines if l["status"] not in ("excluded",)])

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
    ws.append([])
    ws.append(["note", "", gaps.get("rec_report_note", ""), "", ""])
    for col, w in zip("ABCDE", (34, 11, 46, 11, 22)):
        ws.column_dimensions[col].width = w

    ws = wb.create_sheet("Excluded")
    _sheet_of_lines(ws, [l for l in lines if l["status"] == "excluded"])

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        wb.save(tmp.name)
        shutil.move(tmp.name, dest)
    return dest


def _sheet_of_lines(ws, lines):
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
                row.append("yes" if l.get("receipt_link") else "MISSING")
            else:
                row.append(l.get(key) or "")
        ws.append(row)
        conf = l.get("confidence")
        if conf in FILLS:
            ws.cell(row=ws.max_row, column=7).fill = FILLS[conf]
    for i, (_, _, w) in enumerate(LEDGER_COLS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"
