"""Stage 4 — deterministic normalization.

Turns a parsed row into a canonical student record (dict). Handles type
coercion, GPA scale normalization, and assembling indexed/scalar columns into
arrays. Works across export layouts via candidate column-name lists.

Protected attributes (race, gender, age, citizenship, phone) are deliberately
NOT mapped — they must never feed an admissions evaluation.

One narrow exception, requested by the grad office: whether the applicant is
Domestic or International. CAS already splits programs that way ("... - US
Students" / "... - International Students"), so it's read off the designation
first and the export's citizenship *status* second. Only the two-word label is
stored; the country of citizenship is never read, and no rule uses the label.
No LLM is involved here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import SCHEMA_VERSION
from .parse import group_indexed

# Candidate column bases per concept (first one with data wins). This is what
# makes the parser tolerant of different exports.
IELTS_BASES = ["ielts_official_overall_band_score", "ielts_overall_band_score"]
TOEFL_TOTAL_BASES = ["toefl_ibt_official_score", "toefl_ibt_result"]
TOEFL_SECTIONS = {
    "listening": ["toefl_ibt_listening"],
    "reading": ["toefl_ibt_reading"],
    "speaking": ["toefl_ibt_speaking"],
    "writing": ["toefl_ibt_writing"],
}
GRE_BASES = ["gre_general_official_overall_result"]
COLLEGE_NAME_BASES = ["transcript_college", "college_name"]
# `college_country_name_*` is what WebAdMIT actually exports; the other name
# came from an earlier sample and never matched a real roster.
COLLEGE_COUNTRY_BASES = ["country_name_of_college", "college_country_name"]
# Per-school degree and major. Only present when the export includes them
# (WebAdMIT "Colleges Attended" fields); first base with data wins.
COLLEGE_DEGREE_BASES = ["college_degree", "college_degree_type", "degree_type",
                        "transcript_degree", "degree_name", "degree"]
COLLEGE_MAJOR_BASES = ["college_major", "college_major_name", "transcript_major",
                       "major_name", "major", "field_of_study"]
# GRE sections: official/converted first, self-reported second.
GRE_SECTION_BASES = {
    "verbal": ["gre_official_verbal_converted", "gre_official_verbal_scaled",
               "gre_official_verbal", "gre_verbal_scaled", "gre_verbal"],
    "quant": ["gre_official_quantitative_converted", "gre_official_quantitative_scaled",
              "gre_official_quantitative", "gre_quantitative_scaled", "gre_quantitative"],
    "writing": ["gre_official_analytical_writing", "gre_official_analytical_writing_scaled",
                "gre_official_analytical_writing_converted",
                "gre_analytical_writing_scaled", "gre_analytical_writing"],
}
GPA_SUMMARY_COLUMNS = {
    "undergraduate": "gpas_by_transcript_cumulative_undergraduate_gpa",
    "overall": "gpas_by_transcript_cumulative_overall_gpa",
    "graduate": "gpas_by_transcript_cumulative_graduate_gpa",
}
BACKGROUND_COLUMNS = ["custom_field_background", "field_of_study", "undergraduate_major"]
COLLEGE_STATE_BASES = ["college_state"]
GPA_BASE = "gpas_by_transcript_gpa"
DESIGNATION_BASES = ["designation"]
LABEL_BASES = ["designation_label"]
STATUS_BASES = ["local_status", "application_status"]
DEPARTMENT_BASES = ["designation_department_name"]
LEVEL_BASES = ["designation_program_level"]
TERM_BASES = ["designation_program_start_term"]
YEAR_BASES = ["designation_program_start_year"]


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    v = str(v).strip().replace("%", "")
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _series(scalars: Dict[str, str], groups: Dict[str, Dict[int, str]],
            base: str) -> Dict[int, str]:
    """Return {index -> value} for a base, whether it's indexed or scalar."""
    if base in groups:
        return groups[base]
    val = scalars.get(base, "")
    return {0: val} if val != "" else {}


def _first_series(scalars, groups, bases: List[str]) -> Dict[int, str]:
    # "Has data" means a non-blank cell. An exported-but-empty column (every
    # applicant gets `toefl_ibt_official_score_0..2`, filled or not) used to
    # win here and hide the next base, which is how a TOEFL 91 sitting in
    # `toefl_ibt_result_0` went missing from the record.
    for b in bases:
        s = _series(scalars, groups, b)
        if any(str(v).strip() for v in s.values()):
            return s
    return {}


def _all_columns(scalars: Dict[str, str], groups: Dict[str, Dict[int, str]]):
    """(column_name, value) for every non-empty cell, indexed or not."""
    for col, val in scalars.items():
        if str(val).strip():
            yield col, str(val).strip()
    for base, series in groups.items():
        for i in sorted(series):
            if str(series[i]).strip():
                yield f"{base}_{i}", str(series[i]).strip()


_PTE = re.compile(r"(^|_)pte(_|$)")
_ELPE = re.compile(r"(^|_)elpe(_|$)")
_SUBSCORE = re.compile(r"literacy|comprehension|conversation|production|listening|"
                       r"reading|speaking|writing|date|percentile")
_AWARD_COL = re.compile(r"award|honou?r|achievement|scholarship|distinction")


def _best_test(scalars, groups, matches) -> Optional[float]:
    """Highest overall score among columns `matches(name)` accepts. Overall/total
    columns win; otherwise subscore columns are skipped, since a section score
    can be higher than the overall and must not be reported as it."""
    cols = [(c, v) for c, v in _all_columns(scalars, groups) if matches(c.lower())]
    overall = [(c, v) for c, v in cols if re.search(r"overall|total", c.lower())]
    pool = overall or [(c, v) for c, v in cols if not _SUBSCORE.search(c.lower())]
    vals = [x for x in (_to_float(v) for _c, v in pool) if x]
    return max(vals) if vals else None


def _residency(programs: List[Dict[str, Any]], scalars: Dict[str, str]) -> Optional[str]:
    for prog in programs:
        name = (prog.get("name") or "").lower()
        if "international" in name:
            return "International"
        if re.search(r"\bu\.?s\.? students\b|\bdomestic\b", name):
            return "Domestic"
    status = (scalars.get("citizenship_status") or "").strip().lower()
    if re.search(r"u\.?s\.? citizen|permanent resident|green card|\bdomestic\b", status):
        return "Domestic"
    if re.search(r"international|non-?resident|foreign|visa", status):
        return "International"
    return None   # "None" / blank: not stated, and not guessed


def _detect_scale(value: float, cfg: dict) -> float:
    forced = cfg.get("gpa", {}).get("gpa_scale")
    if forced:
        return float(forced)
    for band in cfg.get("gpa", {}).get("auto_detect_thresholds", []):
        if value <= band["max"]:
            return float(band["scale"])
    return 100.0


def _normalize_gpa(raw: float, cfg: dict) -> Dict[str, float]:
    scale = _detect_scale(raw, cfg)
    norm = round(min(raw / scale * 4.0, 4.0), 2)
    return {"raw": raw, "scale": scale, "normalized_4": norm}


def build_record(row: Dict[str, str], cfg: dict, source_file: str,
                 source_row: int) -> Dict[str, Any]:
    scalars, groups = group_indexed(row)
    warnings: List[str] = []

    first = scalars.get("first_name", "").strip()
    last = scalars.get("last_name", "").strip()
    email = scalars.get("email", "").strip()

    # identifier: cas_id -> email -> generated. cas_id is the cross-document
    # merge key, so flag when it is missing.
    has_cas = bool(scalars.get("cas_id", "").strip())
    cas_id = scalars.get("cas_id", "").strip() or email or f"ROW-{source_row}"
    if not has_cas and email:
        warnings.append("No cas_id column; keyed by email — add cas_id for reliable "
                        "cross-document merging (transcripts, SOPs, LORs).")
    elif not has_cas and not email:
        warnings.append("No cas_id or email; generated a row-based id (cannot merge "
                        "other documents to this student).")

    rec: Dict[str, Any] = {"schema_version": SCHEMA_VERSION, "cas_id": cas_id}

    # ---- personal ----
    personal = {}
    if first:
        personal["first_name"] = first
    if last:
        personal["last_name"] = last
    if first or last:
        personal["full_name"] = " ".join(p for p in [first, last] if p)
    if email:
        personal["email"] = email
    if personal:
        rec["personal"] = personal

    # ---- english proficiency ----
    eng: Dict[str, Any] = {}
    ielts_series = _first_series(scalars, groups, IELTS_BASES)
    ielts = []
    for i in sorted(ielts_series):
        band = _to_float(ielts_series[i])
        if band is not None:
            ielts.append({"attempt": i, "overall_band": band})
    if ielts:
        eng["ielts"] = ielts
        eng["best_ielts_overall"] = max(a["overall_band"] for a in ielts)

    total_series = _first_series(scalars, groups, TOEFL_TOTAL_BASES)
    section_series = {k: _first_series(scalars, groups, b) for k, b in TOEFL_SECTIONS.items()}
    toefl = []
    idx_union = set(total_series) | {i for s in section_series.values() for i in s}
    for i in sorted(idx_union):
        attempt: Dict[str, Any] = {"attempt": i}
        tot = _to_float(total_series.get(i, ""))
        if tot is not None:
            attempt["total"] = tot
        for key, s in section_series.items():
            val = _to_float(s.get(i, ""))
            if val is not None:
                attempt[key] = val
        if len(attempt) > 1:
            toefl.append(attempt)
    if toefl:
        eng["toefl"] = toefl
        totals = [a["total"] for a in toefl if "total" in a]
        if totals:
            eng["best_toefl_total"] = max(totals)
    if eng:
        rec["english_proficiency"] = eng

    # ---- GRE (stored loosely, not scored) ----
    gre_series = _first_series(scalars, groups, GRE_BASES)
    gre_results = [v for v in (_to_float(gre_series[i]) for i in sorted(gre_series))
                   if v is not None]
    if gre_results:
        rec["gre_results"] = gre_results
        if not any(260 <= v <= 340 for v in gre_results):
            warnings.append("GRE value(s) present but none are a valid 0-340 total "
                            "(looks like section scores); not scored.")

    # ---- education (college name + gpa, paired by index) ----
    name_series = _first_series(scalars, groups, COLLEGE_NAME_BASES)
    country_series = _first_series(scalars, groups, COLLEGE_COUNTRY_BASES)
    state_series = _first_series(scalars, groups, COLLEGE_STATE_BASES)
    degree_series = _first_series(scalars, groups, COLLEGE_DEGREE_BASES)
    major_series = _first_series(scalars, groups, COLLEGE_MAJOR_BASES)
    gpa_series = _series(scalars, groups, GPA_BASE)
    education = []
    gpa_scale_inferred = False
    for i in sorted(set(name_series) | set(gpa_series) | set(country_series)
                    | set(state_series) | set(degree_series) | set(major_series)):
        entry: Dict[str, Any] = {"index": i}
        if name_series.get(i, "").strip():
            entry["college_name"] = name_series[i].strip()
        if country_series.get(i, "").strip():
            entry["country"] = country_series[i].strip()
        if state_series.get(i, "").strip():
            entry["state"] = state_series[i].strip()
        if degree_series.get(i, "").strip():
            entry["degree"] = degree_series[i].strip()
        if major_series.get(i, "").strip():
            entry["major"] = major_series[i].strip()
        gpa_raw = _to_float(gpa_series.get(i, ""))
        if gpa_raw is not None:
            entry["gpa"] = _normalize_gpa(gpa_raw, cfg)
            gpa_scale_inferred = not cfg.get("gpa", {}).get("gpa_scale")
        if len(entry) > 1:
            education.append(entry)
    if education:
        rec["education"] = education
        if any("gpa" in e for e in education) and gpa_scale_inferred:
            warnings.append("GPA scale was auto-detected from the value (e.g. 3.8 is "
                            "read as /4.0 even if the transcript uses /5.0) — verify, "
                            "or set gpa.gpa_scale in config/csv_field_map.yaml.")

    # ---- programs applied to (designation_* and friends) ----
    desig_series = _first_series(scalars, groups, DESIGNATION_BASES)
    label_series = _first_series(scalars, groups, LABEL_BASES)
    status_series = _first_series(scalars, groups, STATUS_BASES)
    dept_series = _first_series(scalars, groups, DEPARTMENT_BASES)
    level_series = _first_series(scalars, groups, LEVEL_BASES)
    term_series = _first_series(scalars, groups, TERM_BASES)
    year_series = _first_series(scalars, groups, YEAR_BASES)
    programs = []
    for i in sorted(set(desig_series) | set(status_series) | set(dept_series)):
        entry: Dict[str, Any] = {"index": i}
        for key, s in [("name", desig_series), ("label", label_series),
                       ("status", status_series), ("department", dept_series),
                       ("level", level_series), ("start_term", term_series),
                       ("start_year", year_series)]:
            v = s.get(i, "").strip()
            if v:
                entry[key] = v
        if len(entry) > 1:
            programs.append(entry)
    if programs:
        rec["programs"] = programs

    # ---- interests (custom_questions about area of specialization) ----
    cq = cfg.get("custom_questions", {})
    areas_match = cq.get("areas_match", "which_area")
    spec_match = cq.get("specialization_match", "what_area_of_specialization")
    areas, spec = [], []
    for col, val in scalars.items():
        if not col.startswith("custom_questions") or not str(val).strip():
            continue
        if spec_match in col:
            spec.append(val.strip())
        elif areas_match in col:
            areas.append(val.strip())
    interests: Dict[str, Any] = {}
    if areas:
        interests["areas"] = sorted(set(areas))
    if spec:
        interests["specialization"] = sorted(set(spec))
    if interests:
        rec["interests"] = interests

    # ---- prescreen fields the grad office asked for ----
    gre: Dict[str, Any] = {}
    official = False
    for section, bases in GRE_SECTION_BASES.items():
        for base in bases:
            vals = [x for x in (_to_float(v) for v in _series(scalars, groups, base).values())
                    if x]
            if vals:
                gre[section] = max(vals)
                official = official or "official" in base
                break
    if gre:
        gre["official"] = official
        rec["gre"] = gre

    gpa_summary = {}
    for key, col in GPA_SUMMARY_COLUMNS.items():
        val = _to_float(scalars.get(col, ""))
        if val:            # CAS writes 0 when it didn't calculate one
            gpa_summary[key] = val
    if gpa_summary:
        rec["gpa_summary"] = gpa_summary

    duolingo = _best_test(scalars, groups, lambda c: "duolingo" in c)
    pte = _best_test(scalars, groups, lambda c: bool(_PTE.search(c)))
    elpe = next((v for c, v in _all_columns(scalars, groups)
                 if _ELPE.search(c.lower()) and "date" not in c.lower()), None)
    if duolingo is not None or pte is not None or elpe:
        eng = rec.setdefault("english_proficiency", {})
        if duolingo is not None:
            eng["best_duolingo"] = duolingo
        if pte is not None:
            eng["best_pte"] = pte
        if elpe:
            eng["elpe"] = elpe

    residency = _residency(rec.get("programs", []), scalars)
    if residency:
        rec["residency"] = residency

    awards = [v for c, v in _all_columns(scalars, groups)
              if _AWARD_COL.search(c.lower()) and "date" not in c.lower()
              and v.lower() not in ("none", "n/a", "na", "no", "0")]
    if awards:
        rec["awards"] = list(dict.fromkeys(awards))

    background = next((scalars[c].strip() for c in BACKGROUND_COLUMNS
                       if scalars.get(c, "").strip()), "")
    if background:
        rec["background"] = background

    courses = [v for c, v in _all_columns(scalars, groups)
               if "course" in c.lower() and not re.search(r"date|transcript", c.lower())]
    if courses:
        rec["course_answers"] = courses

    # ---- warnings ----
    # Domestic applicants don't sit an English test, so its absence is only
    # worth a flag for everyone else.
    if "english_proficiency" not in rec and rec.get("residency") != "Domestic":
        warnings.append("No IELTS or TOEFL score found.")
    if not any("gpa" in e for e in rec.get("education", [])):
        warnings.append("No transcript GPA found.")

    rec["meta"] = {
        "source_file": source_file,
        "source_row": source_row,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "validation_status": "valid_with_warnings" if warnings else "valid",
        "warnings": warnings,
    }
    return rec
