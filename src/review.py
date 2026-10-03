"""Stage 6 — the review pass: enrich the roster, take the call, gate the cards.

The flow this supports, in order:

  1. `write_review_sheet` writes a copy of the uploaded roster with two columns
     added: `undergrad_degree`, pulled out of the applicant's packet, and a
     blank `status` cell with an accept / reject / tbd dropdown.
  2. The reviewer works through that sheet in Excel and marks each row.
  3. They re-upload the marked sheet. `status_from_row` reads the column back
     onto the record, and from then on only `tbd` applicants get a summary card
     surfaced (see `run.reindex` and `app.summaries`) — accept and reject are
     already decided, so a card for them is reading nobody needs to do.

**Degree comes from the packet only.** The roster export carries
`college_name_*` and a free-text `custom_field_background`, but neither states
a degree — background is a field of study, and on the sample roster it's blank
for a third of rows. Inferring "Bachelor of Science" from "Electrical
Engineering" would put a guess in a column a reviewer reads as fact, so when
there's no packet the cell stays empty and the reviewer can see that it's
missing rather than trusting a filled-in guess.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEGREE_COLUMN = "undergrad_degree"
STATUS_COLUMN = "status"
# Clickable link to the applicant's page in the app (asked for by the grad
# office so a spreadsheet sent to a group opens straight to each profile).
LINK_COLUMN = "summary_link"
STATUSES = ("accept", "reject", "tbd")

# Headers accepted when reading a marked-up sheet. `status` is the one this app
# writes; the others are here so a column the reviewer added by hand, or a sheet
# from an earlier build, still comes back in.
STATUS_HEADERS = ("status", "decision", "review_status")

# The record field. Not `status`: the roster already carries `local_status_*`
# ("Final ADMITS for ADMI") — the university's own decision — and one word
# meaning two different verdicts in the same record is a bug waiting to happen.
STATUS_FIELD = "review_status"

# What a reviewer might actually type in a hurry. Anything unrecognised is
# treated as "not yet decided" rather than guessed at.
_SYNONYMS = {
    "accept": "accept", "accepted": "accept", "a": "accept", "admit": "accept",
    "admitted": "accept", "yes": "accept", "y": "accept", "in": "accept",
    "offer": "accept",
    # "rejected" was missing here and read as undecided — the reviewer would
    # have marked the row, seen nothing happen, and had no idea why.
    "reject": "reject", "rejected": "reject", "r": "reject", "deny": "reject",
    "denied": "reject", "no": "reject", "n": "reject", "out": "reject",
    "tbd": "tbd", "t": "tbd", "maybe": "tbd", "hold": "tbd", "review": "tbd",
    "undecided": "tbd", "?": "tbd",
}

# Bachelor-level credentials. Spelled-out words are matched case-insensitively;
# the bare abbreviations are matched case-sensitively on purpose, because "BS"
# lower-cased collides with ordinary words ("...and be a part of...", "labs")
# and a false match here becomes a wrong degree in the reviewer's column.
_BACHELOR_WORDS = re.compile(
    r"\b(bachelor(?:'?s)?|baccalaureate|b\.\s?s\.?|b\.\s?a\.?|b\.\s?e\.?|"
    r"b\.\s?sc|b\.\s?eng|b\.\s?tech)\b", re.I)
_BACHELOR_ABBR = re.compile(r"\b(BS|BA|BE|BSc|BEng|BTech|B\.?Tech)\b")
_GRADUATE_ONLY = re.compile(r"\b(master|m\.\s?s\.?|m\.\s?eng|m\.\s?tech|ph\.?\s?d|doctor)\b", re.I)

# Transcript legends and disclaimers mention degrees without awarding one:
# "NDC = Not for Undergraduate degree credit", "credit only", "not a degree".
# Cropping from the degree word there produced "Undergraduate degree credit"
# as somebody's qualification. ("undergraduate degree" was also dropped from
# the pattern above — on its own it names no credential.)
_BOILERPLATE = re.compile(r"\b(not for|not a degree|degree credit|credit only|"
                          r"if applicable|n/?a)\b|=", re.I)

# A degree line is a credential, not a paragraph about one. Anything longer is
# almost always a sentence that happens to mention a degree.
_MAX_DEGREE_CHARS = 90


def _clean(line: Any) -> str:
    return " ".join(str(line or "").split()).strip(" .,;:—-•·*")


def _is_degree_line(line: str) -> bool:
    if not line or len(line) > _MAX_DEGREE_CHARS * 2:
        return False
    if not (_BACHELOR_WORDS.search(line) or _BACHELOR_ABBR.search(line)):
        return False
    if _BOILERPLATE.search(line):
        return False
    # "M.S. in EE, Bachelor of Science" still counts; a line that only names a
    # graduate degree does not.
    return True


def _degree_lines(unified: Dict[str, Any]) -> Iterable[str]:
    """Packet text, most structured first: the résumé's parsed education block,
    then the raw résumé, then the transcript."""
    sources = unified.get("sources") or {}
    resume = sources.get("resume") or {}
    profile = ((resume.get("analysis") or {}).get("profile") or {})
    for line in profile.get("education", []) or []:
        yield _clean(line)
    for key in ("resume", "scores"):
        doc = sources.get(key) or {}
        for line in str(doc.get("text") or "").splitlines():
            yield _clean(line)


# Where the credential ends: a dash clause, a sentence break, or a column gap.
# Packet lines run the degree together with whatever sat beside it in the
# original table ("...He/She passed 4-years B.Tech (Electrical Engineering) -
# with Honours for..."), and the reviewer wants the credential, not the row.
_TAIL_BREAK = re.compile(r"\s+[-–—]\s+|[.;:,]\s|\s{2,}")


def _crop(line: str) -> str:
    """Cut a packet line down to the credential itself."""
    match = _BACHELOR_WORDS.search(line) or _BACHELOR_ABBR.search(line)
    if not match:
        return ""
    tail = _TAIL_BREAK.split(line[match.start():])[0].strip(" ,;:—-")
    if len(tail) > _MAX_DEGREE_CHARS:
        tail = tail[:_MAX_DEGREE_CHARS].rsplit(" ", 1)[0]
    return tail


def _names_a_field(degree: str) -> bool:
    """True when the line says what the degree is *in*, not just that there is
    one. "Bachelor of Eng" is a credential; "B.Tech (Electrical Engineering)"
    is a credential a reviewer can use."""
    return "(" in degree or "[" in degree or len(degree.split()) >= 4


def undergrad_degree(unified: Dict[str, Any]) -> str:
    """The applicant's bachelor-level credential as their packet states it.

    Takes the first line that names both the degree and its field; falls back
    to the first bachelor line of any kind when nothing in the packet names a
    field. Résumé before transcript, since a transcript mentions degrees in
    boilerplate ("Not for Undergraduate degree credit") as often as in earnest.

    Empty when no packet is on file or no line names a bachelor's — an empty
    cell is a fact the reviewer can act on, a guessed one isn't.
    """
    fallback = ""
    for line in _degree_lines(unified):
        if not _is_degree_line(line):
            continue
        if _GRADUATE_ONLY.search(line) and not (
                _BACHELOR_WORDS.search(line) or _BACHELOR_ABBR.search(line)):
            continue
        degree = _crop(line)
        if not degree:
            continue
        if _names_a_field(degree):
            return degree
        fallback = fallback or degree
    return fallback


def normalize_status(value: Any) -> str:
    """Map whatever the reviewer typed onto accept / reject / tbd, or "" if it
    isn't one of them. Never guesses: an unreadable cell is undecided."""
    text = str(value or "").strip().lower().rstrip(".")
    return _SYNONYMS.get(text, "")


def status_from_row(row: Dict[str, str]) -> str:
    """Read the status column out of a raw spreadsheet row, whatever case or
    spacing the header came back in."""
    for key, value in row.items():
        if str(key).strip().lower().replace(" ", "_") in STATUS_HEADERS:
            return normalize_status(value)
    return ""


def filter_for_review(entries: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Narrow a list of index entries to the applicants still needing a read.

    Returns (entries, filtering). Before anyone has been marked, nothing is
    hidden — filtering to `tbd` on a roster nobody has marked yet would show an
    empty page and look broken. Once any status exists, the review pass has
    happened and accept/reject rows drop out.
    """
    if not any(e.get(STATUS_FIELD) for e in entries):
        return list(entries), False
    return [e for e in entries if e.get(STATUS_FIELD) == "tbd"], True


def degree_coverage(entries: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """How much of the degree column actually got filled, and why not.

    A blank cell has two very different causes — no packet was uploaded, or the
    packet never names a bachelor's — and a reviewer needs to know which before
    deciding whether to chase the file or read it themselves.

    Takes **index entries**, not full records: the degree and the has-packet
    flag are cached on write. Reading every record to count them meant pulling
    every SOP and letter through memory to render a one-line stat.
    """
    total = with_degree = no_packet = 0
    for entry in entries:
        total += 1
        if entry.get("undergrad_degree"):
            with_degree += 1
        elif not entry.get("has_packet"):
            no_packet += 1
    return {"total": total, "with_degree": with_degree, "no_packet": no_packet,
            "packet_no_degree": total - with_degree - no_packet}


def source_spreadsheet(entries: Iterable[Dict[str, Any]], raw_dir: str) -> Optional[str]:
    """The roster file these applicants came from, if it's still on disk.

    The store caches each applicant's originating file on the index entry, so
    the review copy can be built on the reviewer's own spreadsheet — all 60-odd
    of their columns intact — without reading every full record to find out.
    """
    names = Counter()
    for entry in entries:
        if entry.get("source_file"):
            names[entry["source_file"]] += 1
    for name, _count in names.most_common():
        path = os.path.join(raw_dir, name)
        if os.path.isfile(path):
            return path
    return None


# ---------------------------------------------------------------- the sheet --

_ID_KEYS = ("cas_id", "first_name", "last_name", "email")
_HEADER_FILL = "EEF1F6"
_INPUT_FILL = "FFF9DB"      # the one column the reviewer is meant to type in
_FONT = "Arial"

_HOW_TO = [
    ("How to review", ""),
    ("", ""),
    ("1.", "Work down the Applicants sheet. One row per applicant."),
    ("2.", f"Put your call in the '{STATUS_COLUMN}' column (the shaded one) — "
           "pick from the dropdown: accept, reject, or tbd."),
    ("3.", "Leave a row blank if you haven't decided yet; blank counts as undecided, not tbd."),
    ("4.", "Save the file and upload it again on the app's home page."),
    ("", ""),
    ("What happens next", "Only the tbd applicants get a summary card. Accept and reject "
                          "rows stay in the record but drop off the summaries page — "
                          "they're already decided."),
    ("", ""),
    (LINK_COLUMN, "Opens the applicant's page in the app (sign in first)."),
    (DEGREE_COLUMN, "Read out of the applicant's packet (résumé, then transcript). "
                    "Blank means no packet was uploaded, or no line in it names a "
                    "bachelor-level degree — it is never inferred from the "
                    "background or college columns."),
    ("Other columns", "Untouched, exactly as they came out of your export. "
                      "Re-uploading this file merges on cas_id; it won't duplicate anyone."),
]


def _norm_header(name: Any) -> str:
    return str(name or "").strip().lower().replace(" ", "_")


def _insert_after(columns: List[str]) -> int:
    """Put the new columns after the last identity column, so the reviewer sees
    a name, then a degree, then the cell to fill — without scrolling past sixty
    columns of test scores."""
    last = -1
    for i, col in enumerate(columns):
        if _norm_header(col) in _ID_KEYS:
            last = i
    return last + 1


def _read_roster(path: str):
    import pandas as pd
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".tsv", ".txt"):
        return pd.read_csv(path, dtype=object, keep_default_na=False,
                           na_filter=False, encoding="utf-8-sig")
    return pd.read_excel(path, dtype=object)


def _roster_frame(entries: List[Dict[str, Any]], raw_dir: str):
    """The reviewer's own spreadsheet when we can find it, else a minimal one
    built from the store so the flow still works after a raw purge."""
    import pandas as pd
    src = source_spreadsheet(entries, raw_dir)
    if src:
        try:
            return _read_roster(src), os.path.basename(src)
        except Exception:      # unreadable roster shouldn't block the review pass
            pass
    rows = [{"cas_id": e.get("cas_id"), "name": e.get("name", ""),
             "email": e.get("email", ""),
             "program": "; ".join(e.get("programs") or [])} for e in entries]
    return pd.DataFrame(rows), ""


def write_review_sheet(entries: List[Dict[str, Any]], raw_dir: str, out_path: str,
                       link_base: Optional[str] = None) -> str:
    """Write the roster with `undergrad_degree` filled in and `status` blank.

    Takes index entries: the degree is cached on write, so building this for a
    few thousand applicants is a lookup rather than a re-parse of every packet.
    The original file is never touched — this is always a new copy, because the
    reviewer's export is the one artifact here that can't be regenerated.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    entries = list(entries)
    frame, _src_name = _roster_frame(entries, raw_dir)
    by_id = {str(e.get("cas_id")): e for e in entries}

    id_col = next((c for c in frame.columns if _norm_header(c) == "cas_id"), None)
    email_col = next((c for c in frame.columns if _norm_header(c) == "email"), None)
    by_email = {str(e.get("email", "")).lower(): e for e in entries if e.get("email")}

    def _record_for(row) -> Dict[str, Any]:
        if id_col is not None:
            rec = by_id.get(str(row[id_col]).strip())
            if rec:
                return rec
        if email_col is not None:
            return by_email.get(str(row[email_col]).strip().lower(), {})
        return {}

    degrees, statuses = [], []
    for _i, row in frame.iterrows():
        rec = _record_for(row)
        degrees.append(rec.get(DEGREE_COLUMN, "") if rec else "")
        statuses.append(rec.get(STATUS_FIELD, "") if rec else "")

    links = []
    if link_base:
        from .prescreen_report import link_for
        for _i, row in frame.iterrows():
            rec = _record_for(row)
            links.append(link_for(link_base, rec["cas_id"]) if rec.get("cas_id") else "")

    columns = [str(c) for c in frame.columns]
    # Drop any previous review columns so re-exporting doesn't stack duplicates.
    keep = [c for c in columns
            if _norm_header(c) not in (DEGREE_COLUMN, STATUS_COLUMN, LINK_COLUMN)]
    at = _insert_after(keep)
    added = [STATUS_COLUMN, DEGREE_COLUMN] + ([LINK_COLUMN] if link_base else [])
    out_columns = keep[:at] + added + keep[at:]

    wb = Workbook()
    ws = wb.active
    ws.title = "Applicants"
    ws.append(out_columns)
    for i, (_idx, row) in enumerate(frame.iterrows()):
        values = {c: row[c] for c in keep}
        values[STATUS_COLUMN] = statuses[i]
        values[DEGREE_COLUMN] = degrees[i]
        if link_base:
            values[LINK_COLUMN] = "Open" if links[i] else ""
        ws.append([values.get(c, "") for c in out_columns])

    header_font = Font(name=_FONT, bold=True)
    body_font = Font(name=_FONT)          # one object, not one per cell: a
    fill = PatternFill("solid", fgColor=_HEADER_FILL)   # 65-column roster is
    for cell in ws[1]:                                  # 325k cells.
        cell.font, cell.fill = header_font, fill
        cell.alignment = Alignment(vertical="center")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = body_font

    if link_base:
        l_idx = out_columns.index(LINK_COLUMN) + 1
        link_font = Font(name=_FONT, color="0563C1", underline="single")
        for i, row in enumerate(ws.iter_rows(min_row=2, min_col=l_idx, max_col=l_idx)):
            if i < len(links) and links[i]:
                row[0].hyperlink = links[i]
                row[0].font = link_font

    d_idx = out_columns.index(STATUS_COLUMN) + 1
    g_idx = out_columns.index(DEGREE_COLUMN) + 1
    last_row = max(ws.max_row, 2)
    dv = DataValidation(type="list", formula1=f'"{",".join(STATUSES)}"',
                        allow_blank=True, showDropDown=False)
    dv.error = "Choose accept, reject, or tbd — or leave it blank for now."
    dv.errorTitle = "Not a status"
    ws.add_data_validation(dv)
    letter = get_column_letter(d_idx)
    dv.add(f"{letter}2:{letter}{last_row}")
    input_fill = PatternFill("solid", fgColor=_INPUT_FILL)
    for row in ws.iter_rows(min_row=2, min_col=d_idx, max_col=d_idx):
        row[0].fill = input_fill

    for idx, name in enumerate(out_columns, start=1):
        width = 46 if idx == g_idx else 13 if idx == d_idx else min(max(len(name) + 2, 12), 28)
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = ws.cell(row=2, column=1)
    ws.auto_filter.ref = ws.dimensions

    # The legend lives on its own sheet rather than in a sample row: a fake
    # applicant in the data would be re-ingested as a real one on re-upload.
    guide = wb.create_sheet("How to review")
    for label, text in _HOW_TO:
        guide.append([label, text])
    guide.column_dimensions["A"].width = 18
    guide.column_dimensions["B"].width = 96
    for row in guide.iter_rows():
        row[0].font = Font(name=_FONT, bold=True)
        if len(row) > 1:
            row[1].font = Font(name=_FONT)
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    guide["A1"].font = Font(name=_FONT, bold=True, size=13)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    wb.save(out_path)
    return out_path
