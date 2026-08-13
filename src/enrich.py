"""Step 1 of the review pass, driven from a folder.

    python -m src.enrich --folder "~/Desktop/Fall 2027 applicants"

Point it at a folder holding the roster spreadsheet and the application packets
(a ZIP, loose PDFs, or both). It ingests everything, then writes
`<roster>_review.xlsx` **beside the original** with the undergrad degree filled
in and a blank accept / reject / tbd `status` column. The reviewer opens that file, marks
it up, and uploads it to the app; from then on only the `tbd` applicants get a
summary card.

The original spreadsheet is read, never written. It's the one artifact in the
folder that can't be regenerated, and a half-written Excel file after a crash
mid-run would be a bad trade for saving a copy.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from .ingest_zip import ingest_packet_folder, ingest_zip
from .review import DEGREE_COLUMN, degree_coverage, write_review_sheet
from .run import run
from .store import Store, db_path_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIELD_MAP = os.path.join(ROOT, "config", "csv_field_map.yaml")

ROSTER_EXTS = (".xlsx", ".xls", ".xlsm", ".csv")
# Files this flow produces itself, plus Excel's lock files — never a roster.
_SKIP = ("_review.xlsx", "_reviewed.xlsx")


def find_roster(folder: str) -> Optional[str]:
    """The spreadsheet in the folder. Most recently modified wins when there
    are several, so re-running after a fresh export picks up the new one."""
    candidates = []
    for entry in os.listdir(folder):
        if entry.startswith(("~$", ".")) or entry.lower().endswith(_SKIP):
            continue
        if entry.lower().endswith(ROSTER_EXTS):
            candidates.append(os.path.join(folder, entry))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def enrich_folder(folder: str, outdir: Optional[str] = None) -> Dict[str, Any]:
    """Ingest a folder and write the review spreadsheet next to its roster."""
    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        raise ValueError(f"Not a folder: {folder}")
    outdir = outdir or os.path.join(ROOT, "data")

    roster = find_roster(folder)
    if not roster:
        raise ValueError(
            f"No spreadsheet found in {folder} — expected one of {', '.join(ROSTER_EXTS)}.")

    result = run(roster, outdir, FIELD_MAP)

    zips = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
            if f.lower().endswith(".zip") and not f.startswith("._")]
    packets = {"pdfs_found": 0, "students_updated": 0, "unmatched_files": []}
    for path in zips:
        rep = ingest_zip(path, outdir)
        packets["pdfs_found"] += rep["pdfs_found"]
        packets["students_updated"] += rep["students_updated"]
        packets["unmatched_files"] += rep["unmatched_files"]
    loose = ingest_packet_folder(folder, outdir)
    packets["pdfs_found"] += loose["pdfs_found"]
    packets["students_updated"] += loose["students_updated"]
    packets["unmatched_files"] += loose["unmatched_files"]

    entries = Store(db_path_for(outdir)).index()
    stem = os.path.splitext(os.path.basename(roster))[0]
    out_path = os.path.join(folder, f"{stem}_review.xlsx")
    write_review_sheet(entries, folder, out_path)

    return {"roster": roster, "review_sheet": out_path, "rows": result["rows"],
            "applicants": len(entries), "packets": packets,
            "coverage": degree_coverage(entries)}


def describe(result: Dict[str, Any]) -> List[str]:
    """Plain-language result, including who has no degree and why — an empty
    column should never leave the reviewer guessing whether it's missing data
    or a broken extractor."""
    cov = result["coverage"]
    lines = [
        f"Roster:        {os.path.basename(result['roster'])} ({result['rows']} rows)",
        f"Packets:       {result['packets']['pdfs_found']} PDF(s) read",
        f"Applicants:    {result['applicants']} on file",
        f"{DEGREE_COLUMN}: {cov['with_degree']} of {cov['total']} filled in",
    ]
    if cov["no_packet"]:
        lines.append(f"               {cov['no_packet']} with no packet on file — "
                     "degree unknown, cell left blank")
    if cov["packet_no_degree"]:
        lines.append(f"               {cov['packet_no_degree']} with a packet but no "
                     "line naming a bachelor's degree — check those by hand")
    unmatched = result["packets"]["unmatched_files"]
    if unmatched:
        lines.append(f"Unmatched:     {len(unmatched)} packet file(s) skipped "
                     f"(e.g. {unmatched[0]})")
    lines.append(f"Review sheet:  {result['review_sheet']}")
    lines.append("Next:          mark each row accept / reject / tbd in the "
                 "'status' column, then upload the file in the app.")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Add undergrad_degree + a blank status column to the "
                    "spreadsheet in a folder")
    ap.add_argument("--folder", required=True,
                    help="folder holding the roster spreadsheet and the packets")
    ap.add_argument("--outdir", default=None, help="where the store lives (default data/)")
    args = ap.parse_args()
    print("\n".join(describe(enrich_folder(args.folder, args.outdir))))


if __name__ == "__main__":
    main()
