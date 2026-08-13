"""Phase-2 document ingestion — deterministic text extraction.

Pulls plain text out of a PDF / DOCX / TXT document and packages it as a source
section (SOP or LOR) to merge into a student's unified record.
No LLM: this only extracts and counts text; interpreting it (motivation,
structured courses) is a later, separate step.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from .analyze import (analyze_text, extract_recommender, extract_test_scores,
                      jaccard_sets, load_keywords, shingles, sketch,
                      sketch_similarity)
from .merge import summary_view, upsert_source
from .store import Store, db_path_for
from .template import render

DOC_TYPES = ("sop", "lor", "resume")
# 'scores' is a section type produced only by packet segmentation (score
# reports / transcripts), not a single-file upload type.
SECTION_TYPES = DOC_TYPES + ("scores",)


def extract_text(path: str) -> str:
    """Extract plain text from .pdf, .docx, or .txt/.md. Raises on unknown type."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from . import ocr
        with open(path, "rb") as f:
            pages, _ = ocr.pdf_page_texts(f.read())   # OCRs scanned pages
        return "\n".join(pages).strip()
    if ext == ".docx":
        import docx
        d = docx.Document(path)
        return "\n".join(p.text for p in d.paragraphs).strip()
    if ext in (".txt", ".md"):
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    raise ValueError(f"Unsupported document type '{ext}' (use .pdf, .docx, or .txt)")


def build_section(doc_type: str, text: str, source_file: str,
                  recommender: Optional[str] = None) -> Dict[str, Any]:
    """Build the source-section dict for a document, including deterministic
    (no-LLM) analysis: detected research areas, skills, quality flags, and — for
    LORs and score reports — structured extraction (recommender, test scores)."""
    if doc_type not in SECTION_TYPES:
        raise ValueError(f"doc_type must be one of {SECTION_TYPES}")

    # 'scores' (score reports / transcripts) gets test-score extraction instead
    # of the SOP-style research-area analysis.
    if doc_type == "scores":
        scores = extract_test_scores(text)
        wc = len(text.split())
        analysis = {"word_count": wc, "test_scores": scores, "flags": []}
        if not scores:
            analysis["flags"].append(
                "Score report/transcript text captured but no scores parsed — verify manually.")
    else:
        analysis = analyze_text(text, doc_type)

    section: Dict[str, Any] = {
        "text": text,
        "word_count": analysis["word_count"],
        "char_count": len(text),
        "excerpt": (text[:240] + "…") if len(text) > 240 else text,
        "source_file": os.path.basename(source_file),
        "analysis": analysis,
    }
    if doc_type == "lor":
        # auto-extract the recommender from the letter when not supplied
        rec = extract_recommender(text) if not recommender else {"name": recommender}
        section["recommender"] = rec.get("name")
        if rec.get("title"):
            section["recommender_title"] = rec["title"]
        if rec.get("organization"):
            section["recommender_organization"] = rec["organization"]
        if not section["recommender"]:
            analysis["flags"].append("LOR has no named recommender — verify authorship.")
    return section


def infer_cas_id(path: str) -> Optional[str]:
    """Infer cas_id from a filename like '<cas_id>_sop.pdf' (text before first '_')."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.split("_")[0] if "_" in stem else None


# How many shortlisted SOPs get an exact comparison. Bounds the work per
# document regardless of cohort size or how alike everyone's essays are.
_DUP_CANDIDATES = 25


def _most_similar_sop(store: Store, this_cas_id: str, text: str):
    """Return (other_cas_id, similarity) for the most similar existing SOP
    above the configured threshold, else None. Deterministic, local.

    Two things keep this usable on a real intake, where it runs once per
    incoming SOP against every SOP already on file:

    * the incoming text is tokenized **once**, not once per comparison — the
      old version re-shingled both sides of every pair, which is the whole cost;
    * candidates whose length is wildly different are skipped. Trigram Jaccard
      is bounded by the length ratio (the union is at least the larger set), so
      a document three times longer can't reach a 0.55 threshold — comparing
      them is arithmetic with a known answer.

    It is still O(n) per document and O(n²) per batch. That is affordable for a
    few thousand; past that this wants a MinHash index, not a bigger constant.
    """
    threshold = (load_keywords().get("thresholds", {}) or {}).get("duplicate_similarity", 0.55)
    mine = shingles(text)
    if not mine:
        return None
    my_sketch = sketch(text)

    # Pass 1 — shortlist from the index. Every applicant's SOP sketch is cached
    # there, so this is integer set work rather than deserializing thousands of
    # records (each carrying full essay text) once per incoming document.
    # The margin is deliberate: the sketch is an estimate, so a candidate is
    # anything plausibly close, not anything already over the line.
    margin = threshold * 0.7
    scored = [(sketch_similarity(my_sketch, other_sketch), cas_id)
              for cas_id, other_sketch in store.sop_sketches()
              if cas_id != this_cas_id]
    # Best estimates first, and only so many: on a cohort where everyone writes
    # the same template every applicant clears the margin, and the exact pass
    # would be a full read per document again. We report the *most* similar
    # match, so the top of the estimated ranking is where it lives.
    candidates = [cas_id for est, cas_id in sorted(scored, reverse=True)
                  if est >= margin][:_DUP_CANDIDATES]

    # Pass 2 — the shortlist only, compared exactly. The number a reviewer ends
    # up reading is a real Jaccard, never the estimate.
    best = None
    for cas_id in candidates:
        other = store.get(cas_id)
        sop = ((other or {}).get("sources", {}) or {}).get("sop")
        if not sop or not sop.get("text"):
            continue
        score = jaccard_sets(mine, shingles(sop["text"]))
        if score >= threshold and (best is None or score > best[1]):
            best = (cas_id, score)
    return best


def ingest_document(path: str, cas_id: str, doc_type: str, outdir: str,
                    recommender: Optional[str] = None) -> Tuple[Dict[str, Any], int]:
    """Extract a document and merge it into student <cas_id>'s unified record.

    Creates the student record if it doesn't exist yet (documents may arrive
    before or after the CSV). The merge is an atomic read-modify-write on the
    store. Returns (unified_record, word_count).
    """
    text = extract_text(path)
    section = build_section(doc_type, text, path, recommender)
    store = Store(db_path_for(outdir))

    # near-duplicate detection (SOPs only) against other applicants — local, no LLM
    if doc_type == "sop":
        dup = _most_similar_sop(store, cas_id, text)
        if dup:
            other, score = dup
            section["analysis"]["flags"].append(
                f"SOP is {int(score*100)}% similar to {other}'s SOP — possible reuse.")

    def _apply(unified):
        upsert_source(unified, doc_type, section, file=os.path.basename(path),
                      warnings=section["analysis"].get("flags"))
        unified["summary"] = render(summary_view(unified))
        return unified

    unified = store.update(cas_id, _apply)
    return unified, section["word_count"]
