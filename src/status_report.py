"""The advisor's status export: one row per applicant, every verdict on it.

Answers "where is everyone?" in a file you can mail, sort or paste into a
meeting — who the spreadsheet marked, who you swiped, whose queue they're in,
and what that professor said.

Values, not formulas. This is a snapshot of a decision process at a moment in
time, so a cell that silently recalculated after someone edited a row would
misrepresent what was true when the report was taken. The live view is the
dashboard; this is the record of it.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Sequence

# Column order, and where each value comes from. Identity first, then the facts
# a reader sorts by, then the three verdicts in the order they happen.
COLUMNS = [
    "cas_id", "name", "email", "programme", "undergrad_degree",
    "gpa", "gpa_scale", "areas",
    "status", "advisor_triage", "professor", "professor_status",
    "professor_decided", "flags", "documents",
]

_WAITING = "waiting"


def _professor_names(professors: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    return {p["id"]: p["name"] for p in professors}


def rows_for(entries: Iterable[Dict[str, Any]],
             professors: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One dict per applicant, in the order the report should read: by
    professor (unassigned last), then by name.

    Built from **index entries** — the cached per-student summary the store
    keeps — not full records. Re-deriving the degree and metrics from every
    record made this export take eleven seconds at five thousand applicants,
    and it was all work already done at ingest.
    """
    names = _professor_names(professors)
    rows = []
    for entry in entries:
        metrics = entry.get("metrics") or {}
        prof_id = entry.get("assigned_to") or ""
        assigned = bool(prof_id and prof_id in names)
        rows.append({
            "cas_id": entry.get("cas_id", ""),
            "name": entry.get("name", ""),
            "email": entry.get("email", ""),
            "programme": "; ".join(entry.get("programs") or []),
            "undergrad_degree": entry.get("undergrad_degree", ""),
            # The sheet's own figure; the scale is blank when the export never
            # stated one, matching what the app shows on screen.
            "gpa": metrics.get("gpa_value", ""),
            "gpa_scale": "" if metrics.get("gpa_assumed") else metrics.get("gpa_scale", ""),
            "areas": ", ".join(metrics.get("areas") or []),
            "status": entry.get("review_status", ""),
            "advisor_triage": entry.get("triage", ""),
            "professor": names.get(prof_id, "") if assigned else "",
            # Only "waiting" once someone is actually in a queue — an applicant
            # nobody has been given isn't waiting on a professor.
            "professor_status": (entry.get("professor_status") or _WAITING) if assigned else "",
            "professor_decided": (entry.get("professor_decided_at") or "")[:10],
            "flags": len(entry.get("warnings") or []),
            "documents": ", ".join(entry.get("sources_present") or []),
        })
    order = {p["id"]: i for i, p in enumerate(professors)}
    return sorted(rows, key=lambda r: (order.get(
        next((pid for pid, nm in names.items() if nm == r["professor"]), ""), len(order)),
        str(r["name"]).lower()))


def summarize(rows: Sequence[Dict[str, Any]],
              professors: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-professor tallies for the second sheet."""
    out = []
    for prof in professors:
        mine = [r for r in rows if r["professor"] == prof["name"]]
        out.append({
            "professor": prof["name"],
            "assigned": len(mine),
            "accepted": sum(1 for r in mine if r["professor_status"] == "accept"),
            "rejected": sum(1 for r in mine if r["professor_status"] == "reject"),
            "waiting": sum(1 for r in mine if r["professor_status"] == _WAITING),
        })
    unassigned = [r for r in rows if not r["professor"]]
    out.append({"professor": "— not assigned —", "assigned": len(unassigned),
                "accepted": "", "rejected": "", "waiting": ""})
    return out


def write_status_report(entries: Iterable[Dict[str, Any]],
                        professors: Sequence[Dict[str, Any]],
                        out_path: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = rows_for(list(entries), professors)
    font, bold = Font(name="Arial"), Font(name="Arial", bold=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Status"
    ws.append(COLUMNS)
    for row in rows:
        ws.append([row[c] for c in COLUMNS])

    fill = PatternFill("solid", fgColor="EEF1F6")
    for cell in ws[1]:
        cell.font, cell.fill = bold, fill
        cell.alignment = Alignment(vertical="center")
    # One pass with iter_rows, and the tint objects built once.
    #
    # `ws[row_number]` looks innocent but recomputes the sheet's max column by
    # scanning every cell, so styling row by row that way is quadratic: at five
    # thousand applicants it was 375 million generator calls and 40 seconds.
    tint = {"accept": PatternFill("solid", fgColor="E6F6EF"),
            "reject": PatternFill("solid", fgColor="FDECEA"),
            _WAITING: PatternFill("solid", fgColor="FDF3DD")}
    status_col = COLUMNS.index("professor_status")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = font
        shade = tint.get(str(row[status_col].value))
        if shade:
            row[status_col].fill = shade

    widths = {"name": 24, "email": 28, "programme": 40, "undergrad_degree": 40,
              "areas": 34, "professor": 20, "documents": 26}
    for i, name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(name, 14)
    ws.freeze_panes = ws.cell(row=2, column=1)
    ws.auto_filter.ref = ws.dimensions

    board = wb.create_sheet("By professor")
    board.append(["professor", "assigned", "accepted", "rejected", "waiting"])
    for line in summarize(rows, professors):
        board.append([line["professor"], line["assigned"], line["accepted"],
                      line["rejected"], line["waiting"]])
    for cell in board[1]:
        cell.font, cell.fill = bold, fill
    for row in board.iter_rows(min_row=2):
        for cell in row:
            cell.font = font
    board.column_dimensions["A"].width = 26
    for col in "BCDE":
        board.column_dimensions[col].width = 12

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    wb.save(out_path)
    return out_path
