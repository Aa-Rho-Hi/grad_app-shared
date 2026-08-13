"""Unified per-student record + merge logic.

A student record accumulates data from multiple source documents, keyed by
cas_id. Each source lives in its own section so provenance stays clear and a
later summarizer can read everything in one place:

    {
      "schema_version": "3.0.0",
      "student_id": "<cas_id>",
      "identity": { full_name, first_name, last_name, email },
      "sources": {
        "application": { programs, education, english_proficiency, gre_results, ... },
        "resume":      { text, word_count, analysis, ... } | null,
        "sop":         { text, word_count, analysis, ... } | null,
        "lors":        [ { text, word_count, recommender, analysis }, ... ]  # one entry per letter
      },
      "provenance": [ { source, file, row, ingested_at } ],
      "warnings":   [ ... ]
    }

Adding a new source type later = parse it to a section dict and call
`upsert_source(unified, "<type>", data, file=...)`. Nothing else changes.
"""
from __future__ import annotations

import re

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

UNIFIED_SCHEMA_VERSION = "3.0.0"

# keys taken from a validated CSV record into the "application" section
_APPLICATION_KEYS = ["programs", "education", "english_proficiency",
                     "gre_results", "experience", "interests"]


def application_section(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: rec[k] for k in _APPLICATION_KEYS if k in rec}


def identity_from(rec: Dict[str, Any]) -> Dict[str, Any]:
    p = rec.get("personal", {})
    return {k: p[k] for k in ("full_name", "first_name", "last_name", "email") if p.get(k)}


def new_student(student_id: str) -> Dict[str, Any]:
    return {
        "schema_version": UNIFIED_SCHEMA_VERSION,
        "student_id": student_id,
        "identity": {},
        "sources": {"application": None, "resume": None, "sop": None,
                    "scores": None, "lors": []},
        "provenance": [],
        "warnings": [],
    }


def merge_identity(dst: Dict[str, Any], new: Dict[str, Any]) -> None:
    """Fill missing identity fields; never overwrite an existing value."""
    for k, v in new.items():
        if v and not dst.get(k):
            dst[k] = v


def upsert_source(unified: Dict[str, Any], source_type: str, data: Dict[str, Any],
                  *, file: str, row: Optional[int] = None,
                  warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Attach/replace a source section and record provenance.

    `lor` appends (multiple letters); every other type replaces its section.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if source_type == "lor":
        # Re-processing the same packet/file must not duplicate letters:
        # an entry from the same source file (or with identical text) is
        # replaced in place, anything genuinely new is appended.
        lors = unified["sources"]["lors"]
        new_file = data.get("source_file")
        new_text = (data.get("text") or "").strip()
        replaced = False
        for i, existing in enumerate(lors):
            same_file = bool(new_file) and existing.get("source_file") == new_file
            same_text = bool(new_text) and (existing.get("text") or "").strip() == new_text
            if same_file or same_text:
                lors[i] = data
                replaced = True
                break
        if not replaced:
            lors.append(data)
    else:
        # field-level merge: keep fields a previous file set, let new
        # non-empty fields win. Re-importing a partial export accumulates
        # rather than wiping the section.
        existing = unified["sources"].get(source_type) or {}
        merged = dict(existing)
        for k, v in data.items():
            if v not in (None, [], {}, ""):
                merged[k] = v
        unified["sources"][source_type] = merged
    unified["provenance"].append({"source": source_type, "file": file,
                                  "row": row, "ingested_at": now})
    for w in (warnings or []):
        if w not in unified["warnings"]:
            unified["warnings"].append(w)
    return unified


def applicant_metrics(unified: Dict[str, Any]) -> Dict[str, Any]:
    """Flat, glanceable metrics for the card figures strip: GPA (/4.0), GRE,
    TOEFL/IELTS, LOR average + count, research-area count, flag count. The
    application sheet is authoritative; packet-parsed figures fill the gaps.
    Reports figures only — no scoring or ranking."""
    import re
    s = unified.get("sources") or {}
    app = s.get("application") or {}
    scores = ((s.get("scores") or {}).get("analysis") or {}).get("test_scores") or {}
    eng = app.get("english_proficiency") or {}
    m: Dict[str, Any] = {}

    # GPA (/4.0) — application transcript GPA first, else a "GPA: x/y" résumé line
    # `gpa4` exists to sort and compare across scales — it is a conversion, and
    # when the export never stated a scale it is a *guess* at a conversion. What
    # gets shown is `gpa_value`: the number exactly as the spreadsheet wrote it.
    assumed = any("GPA scale was auto-detected" in w
                  for w in (unified.get("warnings") or []))
    gvals = [e for e in (app.get("education") or [])
             if e.get("gpa") and "normalized_4" in e["gpa"]]
    if gvals:
        best = max(gvals, key=lambda e: e["gpa"]["normalized_4"])
        m["gpa4"] = round(best["gpa"]["normalized_4"], 2)
        raw = best["gpa"].get("raw")
        if raw is not None:
            m["gpa_value"] = f"{raw:g}"
            m["gpa_assumed"] = assumed
            if "scale" in best["gpa"]:
                m["gpa_scale"] = f"{best['gpa']['scale']:g}"
                m["gpa_raw"] = f"{raw:g}/{best['gpa']['scale']:g}"
    else:
        prof = ((s.get("resume") or {}).get("analysis") or {}).get("profile") or {}
        for line in prof.get("education", []):
            mm = re.search(r"gpa[:\s]*([0-9](?:\.\d{1,2})?)\s*/\s*(\d{1,3}(?:\.\d)?)", line, re.I)
            if mm:
                raw, scale = float(mm.group(1)), float(mm.group(2))
                m["gpa4"] = round(min(raw / scale * 4.0, 4.0), 2) if scale else raw
                # The résumé wrote "3.36/4.3" — the scale is stated, not guessed.
                m["gpa_raw"] = f"{mm.group(1)}/{mm.group(2)}"
                m["gpa_value"] = mm.group(1)
                m["gpa_scale"] = mm.group(2)
                m["gpa_assumed"] = False
                break
            mm = re.search(r"gpa[:\s]*([0-4]\.\d{1,2})\b(?!\s*/)", line, re.I)
            if mm and float(mm.group(1)) <= 4.3:
                m["gpa4"] = round(float(mm.group(1)), 2)
                m["gpa_raw"] = f"{mm.group(1)}/4.0"
                m["gpa_value"] = mm.group(1)
                m["gpa_assumed"] = True      # a bare number on a résumé line
                break

    # GRE — application sheet, else parsed from the packet
    if app.get("gre_results"):
        m["gre"] = {"raw": app["gre_results"]}
    elif scores.get("gre_verbal") is not None or scores.get("gre_quant") is not None:
        m["gre"] = {"v": scores.get("gre_verbal"), "q": scores.get("gre_quant"),
                    "awa": scores.get("gre_awa"), "total": scores.get("gre_total")}

    # English proficiency — application sheet, else parsed from the packet
    toefl = eng.get("best_toefl_total") or scores.get("toefl_total")
    ielts = eng.get("best_ielts_overall") or scores.get("ielts_overall")
    if toefl:
        m["toefl"] = round(float(toefl))
    if ielts:
        m["ielts"] = float(ielts)

    # Letters of recommendation
    lors = s.get("lors") or []
    likert = next((l.get("likert") for l in lors if l.get("likert")), None)
    roster = next((l.get("recommenders") for l in lors if l.get("recommenders")), None)
    if likert and likert.get("overall_average") is not None:
        m["lor_avg"] = likert["overall_average"]
    m["lor_count"] = (len(roster) if roster
                      else (likert.get("count") if likert else len(lors)))

    # Research areas (count) + flags
    areas: set = set()
    for k in ("sop", "resume"):
        areas.update(((s.get(k) or {}).get("analysis") or {}).get("detected_areas") or [])
    m["areas"] = sorted(areas)
    m["flags"] = len(unified.get("warnings") or [])
    return m


def sources_present(unified: Dict[str, Any]) -> List[str]:
    present = [s for s in ("application", "resume", "sop", "scores")
              if unified["sources"].get(s)]
    if unified["sources"].get("lors"):
        present.append(f"lors×{len(unified['sources']['lors'])}")
    return present


def _norm_name(n: str) -> str:
    """Fold a name for comparison: case, punctuation and spacing all vary by
    source. The roster writes "CHEN-NI LIU"; a packet filename gives "Chen Ni
    Liu". Only the dot was folded before, so those two never matched and the
    same applicant could sit in the store twice."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (n or "").lower()).split())


def likely_duplicate_groups(entries: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Records that look like the same person under different ids.

    Not merged automatically: two numeric `cas_id`s are two applications as far
    as the store is concerned, and folding them would be irreversible guesswork
    — two people really can share a name. Flagged instead, so the reviewer sees
    "this is the same person twice" rather than quietly reading them as two.
    """
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        key = _norm_name(entry.get("name") or "")
        if key:
            by_name.setdefault(key, []).append(entry)
    groups = []
    for members in by_name.values():
        ids = {m.get("cas_id") for m in members}
        if len(ids) > 1:
            groups.append(sorted(members, key=lambda m: str(m.get("cas_id"))))
    return groups


def _is_cas_id(key: str) -> bool:
    return key.isdigit() and len(key) >= 4


# NOTE: reconciliation of duplicate records (email-keyed vs cas_id-keyed) now
# lives in the storage layer as `Store.reconcile()` (src/store.py), so it runs
# inside a single database transaction. The helpers above (`_norm_name`,
# `_is_cas_id`) and `merge_identity` / `summary_view` are reused there.


def summary_view(unified: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a unified record into the shape the summary template expects."""
    app = unified["sources"].get("application") or {}
    s = unified["sources"]
    return {
        **app,
        "cas_id": unified["student_id"],
        "personal": unified.get("identity", {}),
        "meta": {"warnings": unified.get("warnings", [])},
        "_documents": {
            "resume": s.get("resume"),
            "sop": s.get("sop"),
            "scores": s.get("scores"),
            "lors": s.get("lors") or [],
        },
        "_ocr": unified.get("ocr"),
    }
