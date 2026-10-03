"""The advisor's status export: one row per applicant, every verdict on it.

Answers "where is everyone?" in a file you can mail, sort or paste into a
meeting: who the spreadsheet marked, who you swiped, which group has them, who
is reviewing, the reviewer's first call and the group leader's final call.

Values, not formulas. This is a snapshot of a decision process at a moment in
time, so a cell that silently recalculated after someone edited a row would
misrepresent what was true when the report was taken. The live view is the
dashboard; this is the record of it.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Sequence

# Column order, and where each value comes from. Identity first, then the facts
# a reader sorts by, then the calls in the order they happen: the spreadsheet
# status, the grad office's swipe, the group, the reviewer's first call, the
# leader's final call.
COLUMNS = [
    "cas_id", "name", "email", "programme", "undergrad_degree",
    "gpa", "gpa_scale", "areas", "prescreen",
    "status", "advisor_triage", "group", "group_leader", "reviewer",
    "first_call", "first_call_date", "first_call_note",
    "final_call", "final_call_date", "final_call_note", "stage",
    "flags", "documents",
]

_WAITING = "waiting"
_NO_GROUP = "— not sent to a group —"


def _professor_names(professors: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    return {p["id"]: p["name"] for p in professors}


def rows_for(entries: Iterable[Dict[str, Any]],
             professors: Sequence[Dict[str, Any]],
             groups: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """One dict per applicant, in the order the report should read: by group
    (in the Groups page's order, not-sent last), then by name.

    Built from **index entries** — the cached per-student summary the store
    keeps — not full records. Re-deriving the degree and metrics from every
    record made this export take eleven seconds at five thousand applicants,
    and it was all work already done at ingest.
    """
    from .groups import STAGE_LABELS, stage
    names = _professor_names(professors)
    by_gid = {g["id"]: g for g in groups}
    order = {g["id"]: i for i, g in enumerate(groups)}
    rows = []
    for entry in entries:
        metrics = entry.get("metrics") or {}
        group = by_gid.get(entry.get("group") or "")
        reviewer = entry.get("assigned_to") or ""
        first = entry.get("professor_status") or (_WAITING if reviewer else "")
        final = entry.get("final_status") or (_WAITING if entry.get("professor_status")
                                              and group else "")
        st = stage(entry)
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
            "prescreen": (entry.get("screening") or {}).get("outcome", ""),
            "status": entry.get("review_status", ""),
            "advisor_triage": entry.get("triage", ""),
            "group": group["name"] if group else (entry.get("group") or ""),
            "group_leader": names.get(group.get("leader", ""), "") if group else "",
            "reviewer": names.get(reviewer, reviewer),
            "first_call": first,
            "first_call_date": (entry.get("professor_decided_at") or "")[:10],
            "first_call_note": entry.get("professor_note", ""),
            "final_call": final,
            "final_call_date": (entry.get("final_decided_at") or "")[:10],
            "final_call_note": entry.get("final_note", ""),
            "stage": STAGE_LABELS[st] if (entry.get("group") or entry.get("triage") == "shortlist") else "",
            "flags": len(entry.get("warnings") or []),
            "documents": ", ".join(entry.get("sources_present") or []),
            "_sort": (order.get(entry.get("group") or "", len(order)),
                      str(entry.get("name", "")).lower()),
        })
    rows.sort(key=lambda r: r.pop("_sort"))
    return rows


def summarize(entries: Sequence[Dict[str, Any]],
              groups: Sequence[Dict[str, Any]],
              professors: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Per-group tallies for the second sheet, from index entries."""
    from .groups import READY_FOR_FINAL, TO_DISTRIBUTE, WITH_REVIEWER, buckets, in_group
    names = _professor_names(professors)
    out = []
    for g in groups:
        mine = in_group(entries, g["id"])
        b = buckets(mine)
        out.append({
            "group": g["name"], "leader": names.get(g.get("leader", ""), ""),
            "sent": len(mine), "to_assign": len(b[TO_DISTRIBUTE]),
            "with_reviewers": len(b[WITH_REVIEWER]),
            "awaiting_final": len(b[READY_FOR_FINAL]),
            "accepted": sum(1 for e in mine if e.get("final_status") == "accept"),
            "rejected": sum(1 for e in mine if e.get("final_status") == "reject"),
        })
    not_sent = [e for e in entries if e.get("triage") == "shortlist" and not e.get("group")]
    out.append({"group": _NO_GROUP, "leader": "", "sent": len(not_sent),
                "to_assign": "", "with_reviewers": "", "awaiting_final": "",
                "accepted": "", "rejected": ""})
    return out


SUMMARY_COLUMNS = ["group", "leader", "sent", "to_assign", "with_reviewers",
                   "awaiting_final", "accepted", "rejected"]


def write_status_report(entries: Iterable[Dict[str, Any]],
                        professors: Sequence[Dict[str, Any]],
                        out_path: str,
                        groups: Sequence[Dict[str, Any]] = ()) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    entries = list(entries)
    rows = rows_for(entries, professors, groups)
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
    status_col = COLUMNS.index("final_call")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = font
            # Notes are typed by people; one starting with "=" must stay text.
            if cell.data_type == "f":
                cell.data_type = "s"
        shade = tint.get(str(row[status_col].value))
        if shade:
            row[status_col].fill = shade

    widths = {"name": 24, "email": 28, "programme": 40, "undergrad_degree": 40,
              "areas": 34, "group": 26, "group_leader": 20, "reviewer": 20,
              "first_call_note": 36, "final_call_note": 36, "stage": 30, "documents": 26}
    for i, name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(name, 14)
    ws.freeze_panes = ws.cell(row=2, column=1)
    ws.auto_filter.ref = ws.dimensions

    board = wb.create_sheet("By group")
    board.append(SUMMARY_COLUMNS)
    for line in summarize(entries, groups, professors):
        board.append([line[c] for c in SUMMARY_COLUMNS])
    for cell in board[1]:
        cell.font, cell.fill = bold, fill
    for row in board.iter_rows(min_row=2):
        for cell in row:
            cell.font = font
    board.column_dimensions["A"].width = 30
    board.column_dimensions["B"].width = 22
    for col in "CDEFGH":
        board.column_dimensions[col].width = 14

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    wb.save(out_path)
    return out_path
