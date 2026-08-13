"""Batch-ingest a ZIP of combined 'Full Application' PDFs — one ZIP, all students.

For every PDF in the ZIP:
  * cas_id (and a best-effort name) come from the filename prefix,
  * the combined PDF is segmented into sections,
  * resume / SOP / LOR sections are kept and analyzed locally,
  * score cards and the raw application form are ignored,
  * everything merges into that student's unified record by cas_id.

Safe by design: validates the ZIP, enforces file-count / size limits, reads
each entry in memory (no disk extraction -> no zip-slip), and only processes
PDFs. No LLM, no network.
"""
from __future__ import annotations

import functools
import io
import json
import os
import zipfile
from typing import Any, Dict, List

from .analyze import (extract_all_recommenders, extract_standardized_tests,
                      load_keywords, merge_recommenders)
from .documents import build_section
from .merge import merge_identity, summary_view, upsert_source
from .packet import cas_id_from_filename, name_from_filename, segment
from .ratings import extract_likert_ratings
from .run import reindex
from .store import Store, db_path_for
from .template import render

MAX_FILES = 5000
MAX_TOTAL_UNCOMPRESSED = 2_000_000_000   # 2 GB
MAX_PDF_BYTES = 60_000_000               # 60 MB per PDF


def _new_report() -> Dict[str, Any]:
    return {
        "pdfs_found": 0, "students_updated": 0, "sections_added": 0,
        "score_sections": 0, "unmatched_files": [], "details": [],
        "scanned_docs": 0, "ocr_pages": 0, "ocr_unavailable_docs": 0,
    }


def _ingest_packets(items, outdir: str, report: Dict[str, Any], progress=None) -> set:
    """Split and merge a batch of packet PDFs, whatever they arrived in.

    `items` is (filename, size_in_bytes, read_bytes) per PDF, so a ZIP member
    and a file sitting loose in a folder go down exactly one code path — the
    packet splitting, OCR, Likert parsing and per-student merge below are
    fiddly enough that a second copy of them would drift within a release.
    """
    kw = load_keywords()
    store = Store(db_path_for(outdir))
    updated_ids: set = set()

    for i, (name, size, read) in enumerate(items):
        if progress:
            progress(i, len(items), f"Reading {name[:60]}")
        cas = cas_id_from_filename(name)
        if not cas:
            report["unmatched_files"].append(name + " (no cas_id in filename)")
            continue
        if size > MAX_PDF_BYTES:
            report["unmatched_files"].append(name + " (file too large)")
            continue

        raw = read()
        try:
            seg = segment(io.BytesIO(raw), kw)
        except Exception as exc:
            report["unmatched_files"].append(f"{name} (unreadable: {exc})")
            continue

        # read the CAS Likert reference ratings (positional, no OCR); cheap
        try:
            likert = extract_likert_ratings(io.BytesIO(raw))
        except Exception:
            likert = {"evaluators": [], "overall_average": None, "count": 0}

        # parse the structured GRE/IELTS/TOEFL tables from the CAS academic-
        # history form (embedded text). The application sheet is authoritative;
        # these fill gaps and are surfaced marked "(parsed — verify)".
        try:
            from pypdf import PdfReader
            form_text = "\n".join((p.extract_text() or "")
                                  for p in PdfReader(io.BytesIO(raw)).pages)
            cas_tests = extract_standardized_tests(form_text)
        except Exception:
            cas_tests = {}

        for sect in seg["sections"]:
            if sect in ("scorecard", "scores"):
                report["score_sections"] += 1
        oi = seg.get("ocr", {})
        added: List[str] = []

        def _apply(unified, seg=seg, name=name, added=added, oi=oi,
                   likert=likert, cas_tests=cas_tests):
            merge_identity(unified["identity"], name_from_filename(name))
            for sect, info in seg["sections"].items():
                if not info.get("text", "").strip():
                    continue
                target = "scores" if sect == "scorecard" else sect
                if target not in ("resume", "sop", "lor", "scores"):
                    continue
                section = build_section(target, info["text"], name)
                section["page_range"] = info.get("page_range")
                if target == "lor":
                    if likert.get("count"):
                        section["likert"] = likert  # ratings for all evaluators
                    likert_names = [e.get("evaluator")
                                    for e in (likert.get("evaluators") or [])
                                    if e.get("evaluator")]
                    section["recommenders"] = merge_recommenders(
                        likert_names, extract_all_recommenders(info["text"]))
                upsert_source(unified, target, section, file=name,
                              warnings=section["analysis"].get("flags"))
                added.append(target)
            # fold the structured GRE/IELTS/TOEFL scores parsed from the
            # application-history form into the scores source (gaps only).
            if cas_tests:
                sc = unified["sources"].get("scores")
                if not sc:
                    sc = build_section("scores", "Standardized tests from the "
                                       "application form.", name)
                    sc["analysis"]["flags"] = []
                    upsert_source(unified, "scores", sc, file=name)
                    sc = unified["sources"]["scores"]
                ts = sc.setdefault("analysis", {}).setdefault("test_scores", {})
                for k, v in cas_tests.items():
                    ts.setdefault(k, v)
                note = ("Score report/transcript text captured but no scores "
                        "parsed — verify manually.")
                if ts and note in unified["warnings"]:
                    unified["warnings"].remove(note)
            # record OCR status explicitly (not as a "problem" warning)
            if oi.get("ocr_used"):
                unified["ocr"] = {"scanned_pages": oi.get("scanned_pages", 0),
                                  "ocr_pages_read": oi["ocr_used"], "source_file": name}
            # only real problems go into warnings (skip the positive "OCR read ..." note)
            for fl in seg.get("flags", []):
                if fl.startswith("OCR read"):
                    continue
                if fl not in unified["warnings"]:
                    unified["warnings"].append(fl)
            unified["summary"] = render(summary_view(unified))
            return unified

        store.update(cas, _apply)        # atomic per-student merge
        updated_ids.add(cas)

        if oi.get("ocr_used"):
            report["scanned_docs"] += 1
            report["ocr_pages"] += oi["ocr_used"]
        if oi.get("ocr_unavailable"):
            report["ocr_unavailable_docs"] += 1

        report["students_updated"] += 1
        report["sections_added"] += len(added)
        report["details"].append({"cas_id": cas, "file": name, "sections": added,
                                  "pages": seg["page_count"],
                                  "ocr_pages": oi.get("ocr_used", 0)})

    if progress:
        progress(len(items), len(items), "Rebuilding summaries")
    return updated_ids


def ingest_zip(zip_path: str, outdir: str, progress=None) -> Dict[str, Any]:
    """Ingest every packet PDF inside a ZIP of combined application packets."""
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("That file is not a valid ZIP archive.")
    report = _new_report()

    with zipfile.ZipFile(zip_path) as zf:
        members = [zi for zi in zf.infolist() if not zi.is_dir()]
        if len(members) > MAX_FILES:
            raise ValueError(f"ZIP has too many files (>{MAX_FILES}).")
        if sum(zi.file_size for zi in members) > MAX_TOTAL_UNCOMPRESSED:
            raise ValueError("ZIP uncompressed size exceeds the safety limit.")
        pdfs = [zi for zi in members
                if zi.filename.lower().endswith(".pdf")
                and not os.path.basename(zi.filename).startswith("._")   # macOS AppleDouble junk
                and "__MACOSX" not in zi.filename]
        report["pdfs_found"] = len(pdfs)
        # Read inside the `with`: each item closes over the open archive.
        items = [(os.path.basename(zi.filename), zi.file_size,
                  functools.partial(zf.read, zi)) for zi in pdfs]
        updated_ids = _ingest_packets(items, outdir, report, progress)

    reindex(outdir, changed=updated_ids)
    return report


def ingest_packet_folder(folder: str, outdir: str, progress=None) -> Dict[str, Any]:
    """Ingest packet PDFs sitting loose in a folder (and one level of
    subfolders), the same way they'd be handled inside a ZIP.

    For the folder-first flow: the reviewer drops the roster and the packets in
    one folder and never opens the browser to get the degree column filled in.
    """
    paths: List[str] = []
    for entry in sorted(os.listdir(folder)):
        full = os.path.join(folder, entry)
        if os.path.isdir(full):
            paths += [os.path.join(full, f) for f in sorted(os.listdir(full))
                      if f.lower().endswith(".pdf") and not f.startswith("._")]
        elif entry.lower().endswith(".pdf") and not entry.startswith("._"):
            paths.append(full)
    if len(paths) > MAX_FILES:
        raise ValueError(f"Folder has too many PDFs (>{MAX_FILES}).")

    report = _new_report()
    report["pdfs_found"] = len(paths)
    items = [(os.path.basename(p), os.path.getsize(p),
              functools.partial(_read_file, p)) for p in paths]
    updated_ids = _ingest_packets(items, outdir, report, progress)
    reindex(outdir, changed=updated_ids)
    return report


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def main() -> None:
    import argparse
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Ingest a ZIP of Full Application PDFs")
    ap.add_argument("--input", required=True, help="path to the .zip")
    ap.add_argument("--outdir", default=os.path.join(root, "data"))
    args = ap.parse_args()
    print(json.dumps(ingest_zip(args.input, args.outdir), indent=2))


if __name__ == "__main__":
    main()
