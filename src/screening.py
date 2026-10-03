"""Prescreen: the grad office's admit rules, applied as flags. No LLM.

The ECEN grad office asked (Deshaun Tolbert, 2026-10-02) for three things on
top of the summaries:

* **Quick Admit, on-campus programs**: "Admit" when the applicant is from the
  Electrical & Computer Engineering department with a GPA of 3.25 or higher;
  everyone else "Further review".
* **MS Distance** (its own file): ECE + GPA 3.25 + ten courses from the approved
  distance list (at most two 400-level, the rest 600/700-level). Thesis
  applicants also need proof a faculty member will advise them, and either the
  employer letter or the unemployment statement in its exact wording.
* **PhD Distance** (its own file): six approved courses with an MS, fourteen
  with a Bachelor's only, same 400-level cap, plus the PhD employer letter.

Every result is a *flag*, never a decision. The outcome is only ever "Admit"
or "Further review", always with the reasons spelled out, and anything a
machine can't honestly confirm (a letterhead, a signature, whether the advisor
is TAMU faculty) is listed under "check by hand" even on an Admit. A person
confirms every row.

All the numbers and wording live in config/screening_rules.yaml and the course
list in config/distance_courses.yaml, so the office can change them without a
code change. `rules_version()` fingerprints both files; cached results computed
under older rules are recomputed on read (see Store.index).

Where the evidence comes from:

* program, GPA, major, test scores: the roster export (authoritative), with the
  packet filling gaps;
* course lists: an application question about courses if the export has one,
  otherwise the packet pages that look like a course plan;
* letters: the packet pages kept at ingest (`packet_evidence`, see
  `evidence_pages`) so the rules can be re-run without re-uploading anything.
"""
from __future__ import annotations

import hashlib
import os
import re
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_YAML = os.path.join(ROOT, "config", "screening_rules.yaml")
COURSES_YAML = os.path.join(ROOT, "config", "distance_courses.yaml")

ADMIT = "Admit"
REVIEW = "Further review"

QUICK_ADMIT = "quick_admit"
MS_DISTANCE = "ms_distance"
PHD_DISTANCE = "phd_distance"
OTHER_DISTANCE = "other_distance"
ON_CAMPUS = "on_campus"
NO_PROGRAM = "no_program"

TRACK_LABELS = {
    QUICK_ADMIT: "Quick Admit (on campus)",
    MS_DISTANCE: "MS Distance",
    PHD_DISTANCE: "PhD Distance",
    OTHER_DISTANCE: "Distance (other degree)",
    ON_CAMPUS: "On campus",
    NO_PROGRAM: "No program on file",
}
# Tracks the office gave rules for. Everything else is summarized, not flagged.
SCREENED = (QUICK_ADMIT, MS_DISTANCE, PHD_DISTANCE)
# Tracks that go to their own spreadsheet.
SEPARATE_FILES = (MS_DISTANCE, PHD_DISTANCE)


# ------------------------------------------------------------------ config --

@lru_cache(maxsize=16)
def _load_cached(path: str, _mtime: int, _size: int) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load(path: str) -> Dict[str, Any]:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    return _load_cached(path, st.st_mtime_ns, st.st_size)


def load_rules(path: Optional[str] = None) -> Dict[str, Any]:
    return _load(path or RULES_YAML)


def load_courses(path: Optional[str] = None) -> Dict[str, str]:
    """{"ECEN 601": "Mathematical Methods in Signal Processing", ...}"""
    raw = (_load(path or COURSES_YAML).get("courses") or {})
    out = {}
    for code, title in raw.items():
        norm = normalize_course(str(code))
        if norm:
            out[norm] = str(title or "")
    return out


def courses_source(path: Optional[str] = None) -> Dict[str, str]:
    data = _load(path or COURSES_YAML)
    return {"url": str(data.get("source_url") or ""),
            "retrieved": str(data.get("retrieved") or "")}


def _stamp(path: str) -> Tuple[str, int, int]:
    try:
        st = os.stat(path)
        return path, st.st_mtime_ns, st.st_size
    except OSError:
        return path, 0, -1


@lru_cache(maxsize=16)
def _fingerprint(a: Tuple[str, int, int], b: Tuple[str, int, int]) -> str:
    h = hashlib.sha256()
    for path, _m, _s in (a, b):
        try:
            with open(path, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"-")
    h.update(_ENGINE_VERSION.encode())
    return h.hexdigest()[:12]


def rules_version(rules_path: Optional[str] = None,
                  courses_path: Optional[str] = None) -> str:
    """Short fingerprint of the rules + course list as they are on disk now.
    Called once per record on every write, so it only re-reads the files
    when their size or modification time changed."""
    return _fingerprint(_stamp(rules_path or RULES_YAML), _stamp(courses_path or COURSES_YAML))


# Bump when the rule *logic* changes, so cached results recompute even though
# the YAML didn't.
_ENGINE_VERSION = "1"


# ------------------------------------------------------------------- text --

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


def _apply_equivalents(text: str, equivalents: Optional[Dict[str, str]]) -> str:
    for word, alt in (equivalents or {}).items():
        text = re.sub(rf"\b{re.escape(str(word).lower())}\b", str(alt).lower(), text)
    return text


def compact(text: str, equivalents: Optional[Dict[str, str]] = None) -> str:
    """Lower-case, letters and digits only. PDF text layers scramble spacing
    ("RELEA SE S TATEMENT"), line breaks and quotes, so letter wording is
    compared on this form: every word must be present and in order, but a
    stray space or a curly quote can't make it fail."""
    low = _apply_equivalents((text or "").lower(), equivalents)
    return _NON_ALNUM.sub("", low)


def _words(text: str, equivalents: Optional[Dict[str, str]] = None) -> List[str]:
    low = _apply_equivalents((text or "").lower(), equivalents)
    return [w for w in _NON_ALNUM.split(low) if w]


_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")   # icon-font bullets from résumé PDFs


def _clip(text: str, limit: int = 160) -> str:
    text = _WS.sub(" ", _PRIVATE_USE.sub(" ", str(text or ""))).strip(" •-*·▪◦●–")
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------- courses --

_COURSE = re.compile(r"(?<![A-Za-z])(ECEN|ECE|ELEN|CSCE)\s*[-–]?\s*(\d{3})(?!\d)", re.I)


def normalize_course(text: str) -> str:
    """"ecen-601" / "ECE 601" / "ECEN601" -> "ECEN 601". ECE is the common
    shorthand for ECEN, so it's read as ECEN."""
    m = _COURSE.search(text or "")
    if not m:
        return ""
    prefix = m.group(1).upper()
    if prefix == "ECE":
        prefix = "ECEN"
    return f"{prefix} {m.group(2)}"


def extract_courses(text: str, prefixes: Optional[Sequence[str]] = None) -> List[str]:
    """Every course code in `text`, normalized, first-seen order, no repeats."""
    allowed = {p.upper() for p in (prefixes or ("ECEN", "ECE", "ELEN", "CSCE"))}
    seen: List[str] = []
    for m in _COURSE.finditer(text or ""):
        if m.group(1).upper() not in allowed:
            continue
        code = normalize_course(m.group(0))
        if code and code not in seen:
            seen.append(code)
    return seen


def _level(code: str) -> int:
    return int(code.split()[-1]) // 100 * 100


def check_courses(listed: Sequence[str], required: int, approved: Dict[str, str],
                  cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the degree-plan rules to a course list.

    Counts toward the requirement: approved 400-level courses (capped at
    `max_400_level`) plus approved 600/700-level courses. Listing more 400-level
    courses than the cap is itself a failed requirement ("they can only list up
    to 2"); a course off the approved list, or a 200/300-level one, simply
    doesn't count and is reported so a reviewer sees why the total is short.
    """
    max400 = int(cfg.get("max_400_level", 2))
    allowed_levels = {int(x) for x in (cfg.get("allowed_levels") or [600, 700])}
    lvl400, upper, not_listed, too_low = [], [], [], []
    for code in listed:
        if code not in approved:
            not_listed.append(code)
        elif _level(code) == 400:
            lvl400.append(code)
        elif _level(code) in allowed_levels:
            upper.append(code)
        else:
            too_low.append(code)
    counted = min(len(lvl400), max400) + len(upper)
    problems = []
    if len(lvl400) > max400:
        problems.append(f"lists {len(lvl400)} 400-level courses ({', '.join(lvl400)}); "
                        f"only {max400} allowed")
    if counted < required:
        problems.append(f"{counted} approved course(s) listed; {required} needed")
    return {
        "listed": list(listed), "required": required, "counted": counted,
        "level_400": lvl400, "level_600_700": upper,
        "not_on_list": not_listed, "below_400": too_low,
        "ok": not problems, "problems": problems,
    }


# ------------------------------------------------------------- the packet --

# Pages worth keeping from a packet for the rules: anything with a course code
# or letter/advisor wording. Matched on the compacted text so CAS's scrambled
# spacing can't hide them.
_EVIDENCE_CUES = ("giveapprovalfor", "notcurrentlyemployed", "disseminate",
                  "employerverification", "letterhead", "formallydeclare",
                  "advisor", "adviser", "supervis", "serveas", "agreeto",
                  "courseplan", "degreeplan", "planofstudy", "plannedcourses",
                  "proposedcourses", "coursesoffered", "courseselection",
                  "distancelearning", "distanceeducation")
_LETTER_CUES = ("giveapprovalfor", "notcurrentlyemployed", "disseminate",
                "formallydeclare", "employerverification")
MAX_EVIDENCE_PAGES = 60


def evidence_pages(pages: Sequence[str], labels: Sequence[str],
                   file: str = "") -> List[Dict[str, Any]]:
    """The packet pages the rules need, kept on the record at ingest.

    Small on purpose: a packet can carry a whole thesis, and the store keeps
    records in memory for exports. A letter can run onto a second page, so the
    page after a letter cue is kept too."""
    keep = set()
    for i, text in enumerate(pages):
        c = compact(text)
        if _COURSE.search(text or "") or any(cue in c for cue in _EVIDENCE_CUES):
            keep.add(i)
            if any(cue in c for cue in _LETTER_CUES) and i + 1 < len(pages):
                keep.add(i + 1)
    out = []
    for i in sorted(keep)[:MAX_EVIDENCE_PAGES]:
        out.append({"file": file, "page": i + 1,
                    "label": labels[i] if i < len(labels) else "",
                    "text": pages[i] or ""})
    return out


def merge_evidence(unified: Dict[str, Any], file: str,
                   pages: List[Dict[str, Any]]) -> None:
    """Replace this file's pages on the record, keep other files' pages."""
    kept = [p for p in (unified.get("packet_evidence") or []) if p.get("file") != file]
    unified["packet_evidence"] = kept + pages


_TRANSCRIPT_CUES = ("transcript", "quality points", "grade points", "credit hours",
                    "hours attempted", "hours earned", "cumulative gpa", "term gpa",
                    "semester gpa", "registrar", "grade point average", "credits earned")
_PLAN_CUES = ("course plan", "degree plan", "plan of study", "planned course",
              "proposed course", "courses offered", "course selection",
              "plan to take", "intend to take", "courses i will take",
              "distance learning", "distance education", "online courses")


def _is_transcript(text: str) -> bool:
    low = (text or "").lower()
    return sum(1 for cue in _TRANSCRIPT_CUES if cue in low) >= 2


def find_course_list(app: Dict[str, Any], pages: Sequence[Dict[str, Any]],
                     prefixes: Sequence[str]) -> Tuple[List[str], str]:
    """(courses, where-they-came-from). Application answers win; then pages
    that read like a course plan; then any non-transcript, non-résumé page with
    three or more course codes. Transcripts and résumés list courses already
    taken, which is the opposite of a plan."""
    answers = "\n".join(app.get("course_answers") or [])
    found = extract_courses(answers, prefixes)
    if found:
        return found, "application course question"

    usable = [p for p in pages
              if p.get("label") != "resume" and not _is_transcript(p.get("text", ""))]
    planned = [p for p in usable
               if any(c in (p.get("text") or "").lower() for c in _PLAN_CUES)
               and extract_courses(p.get("text", ""), prefixes)]
    chosen = planned or [p for p in usable
                         if len(extract_courses(p.get("text", ""), prefixes)) >= 3]
    courses: List[str] = []
    for p in chosen:
        for code in extract_courses(p.get("text", ""), prefixes):
            if code not in courses:
                courses.append(code)
    if not courses:
        return [], ""
    where = ", ".join(f"p.{p['page']}" for p in chosen)
    return courses, f"packet {where}"


# ---------------------------------------------------------------- letters --

def _template_regex(template: str, equivalents: Dict[str, str]) -> "re.Pattern[str]":
    parts = [compact(p, equivalents) for p in template.split("[NAME]")]
    body = "(.{1,80}?)".join(re.escape(p) for p in parts)
    return re.compile(body)


def _run_coverage(target: List[str], words: List[str], min_run: int = 3) -> float:
    """Share of the template's words found in order in runs of 3+ words.
    Runs, not single words: any page of English shares "the", "to" and "of"
    with the template, but not three-word stretches of it."""
    if not target or not words:
        return 0.0
    sm = SequenceMatcher(None, target, words, autojunk=False)
    hit = sum(b.size for b in sm.get_matching_blocks() if b.size >= min_run)
    return hit / len(target)


def _name_ok(found: Iterable[str], applicant: str) -> bool:
    tokens = [t for t in _words(applicant) if len(t) >= 3]
    if not tokens:
        return True
    return all(any(t in f for t in tokens) for f in found)


def match_letter(pages: Sequence[Dict[str, Any]], template: str,
                 letters_cfg: Dict[str, Any], applicant_name: str = "") -> Dict[str, Any]:
    """Look for `template` in the packet pages.

    status: "exact" (every word, in order: the requirement is met), "close"
    (most of the wording is there but not all of it: a person compares it),
    or "missing". Pages are also tried two at a time, since a letter can run
    across a page break.
    """
    eq = {str(k).lower(): str(v).lower() for k, v in
          (letters_cfg.get("equivalent_words") or {}).items()}
    close_at = float(letters_cfg.get("close_match", 0.85))
    rx = _template_regex(template, eq)
    target = [w for w in _words(template.replace("[NAME]", " "), eq)]

    # Single pages first, so a letter that fits on one page is reported on
    # that page; then adjacent pairs for one that runs over a page break.
    ordered = sorted(pages, key=lambda p: (p.get("file", ""), p.get("page", 0)))
    windows: List[Tuple[str, str]] = [(f"p.{p.get('page')}", p.get("text") or "")
                                      for p in ordered]
    for p, nxt in zip(ordered, ordered[1:]):
        if nxt.get("file") == p.get("file") and nxt.get("page") == p.get("page", 0) + 1:
            windows.append((f"p.{p.get('page')}-{nxt.get('page')}",
                            (p.get("text") or "") + "\n" + (nxt.get("text") or "")))

    for where, text in windows:
        m = rx.search(compact(text, eq))
        if m:
            names = [g for g in m.groups() if g]
            out = {"status": "exact", "where": where}
            if applicant_name and names and not _name_ok(names, applicant_name):
                out["name_mismatch"] = ", ".join(sorted(set(names)))
            return out

    best = {"status": "missing"}
    tset = set(target)
    for where, text in windows:
        words = _words(text, eq)
        if len(tset & set(words)) < 0.5 * len(tset):
            continue     # cheap filter before the O(n*m) comparison
        cov = _run_coverage(target, words)
        if cov >= close_at and cov > best.get("coverage", 0):
            best = {"status": "close", "where": where, "coverage": round(cov, 2)}
    return best


# Faculty confirming they'll advise. Narrow on purpose: an SOP saying "I hope
# Dr. X will be my advisor" is a wish, not proof, so SOP and résumé pages are
# never searched.
_ADVISOR_PATTERNS = [
    re.compile(r"\b(agree[sd]?|agreeing|willing|happy|glad|pleased|delighted|consent\w*|"
               r"commit\w*|confirm\w*|accept\w*)\b[^.]{0,80}?\b(to\s+)?(serve|act|be)\s+"
               r"(as\s+)?(his|her|their|the|a|an|your|[a-z]+'s)?\s*(\w+\s+){0,3}?"
               r"(advisor|adviser|supervisor|committee chair|chair)\b"),
    re.compile(r"\b(i|we)\s+(will|shall|am going to)\s+(serve|act)\s+as\s+(\w+\s+){0,4}?"
               r"(advisor|adviser|supervisor|chair)\b"),
    re.compile(r"\b(agree[sd]?|willing|consent\w*)\s+to\s+(advise|supervise|mentor)\b"),
    re.compile(r"\baccept\w*\b[^.]{0,60}?\b(as|into)\s+(my|our|his|her|a)\s+"
               r"(\w+\s+){0,2}?(student|advisee)\b"),
]


_NEGATED = re.compile(r"\b(not|unable|cannot|can't|won't|decline\w*)\b")


def find_advisor_proof(pages: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    for p in sorted(pages, key=lambda p: (p.get("file", ""), p.get("page", 0))):
        if p.get("label") in ("sop", "resume"):
            continue
        low = _WS.sub(" ", (p.get("text") or "").lower())
        for rx in _ADVISOR_PATTERNS:
            m = next((m for m in rx.finditer(low)
                      if not _NEGATED.search(low[max(0, m.start() - 25):m.start() + 30])), None)
            if m:
                start = max(0, m.start() - 60)
                return {"status": "found", "where": f"p.{p.get('page')}",
                        "snippet": _clip(low[start:m.end() + 40], 180)}
    return {"status": "missing"}


# ------------------------------------------------------- applicant facts --

def classify(programs: Sequence[Dict[str, Any]], rules: Dict[str, Any]) -> Dict[str, Any]:
    """Which rule set applies, read off the CAS designation name."""
    tcfg = rules.get("tracks") or {}
    distance_words = [w.lower() for w in (tcfg.get("distance_words") or ["distance", "online"])]
    quick_words = [w.lower() for w in (tcfg.get("quick_admit_words") or ["quick admit"])]
    named = [p for p in programs if p.get("name") or p.get("label")]
    if not named:
        return {"track": NO_PROGRAM, "program": "", "thesis": None, "others": []}

    def _one(prog: Dict[str, Any]) -> Dict[str, Any]:
        name = " ".join(x for x in (prog.get("name"), prog.get("label")) if x)
        low = name.lower()
        thesis = None
        if re.search(r"non[\s-]?thesis", low):
            thesis = False
        elif "thesis" in low:
            thesis = True
        is_phd = bool(re.search(r"(?<![a-z])ph\.?\s?d(?![a-z])|doctor", low))
        is_ms = bool(re.search(r"(?<![a-z])m\.?\s?s(?![a-z])|master of science", low))
        if any(w in low for w in distance_words):
            track = PHD_DISTANCE if is_phd else MS_DISTANCE if is_ms else OTHER_DISTANCE
        elif any(w in low for w in quick_words):
            track = QUICK_ADMIT
        else:
            track = ON_CAMPUS
        return {"track": track, "program": prog.get("name") or name, "thesis": thesis}

    first = _one(named[0])
    first["others"] = [p.get("name") for p in named[1:]
                       if _one(p)["track"] != first["track"] and p.get("name")]
    return first


def _ece_match(text: str, rules: Dict[str, Any]) -> bool:
    low = (text or "").lower()
    if any(re.search(rf"\b{re.escape(x.lower())}\b", low) for x in rules.get("ece_exclude") or []):
        return False
    return any(re.search(rf"\b{re.escape(t.lower())}\b", low)
               for t in rules.get("ece_majors") or [])


_MAJOR_LINE = re.compile(r"\b(?:major|program|field of study)\s*[:\-]\s*([A-Za-z&,./ ]{4,80})", re.I)


def applicant_majors(unified: Dict[str, Any]) -> Tuple[List[str], str]:
    """The applicant's own field(s), most authoritative source first."""
    from .review import undergrad_degree
    sources = unified.get("sources") or {}
    app = sources.get("application") or {}
    edu = app.get("education") or []
    majors = [e["major"] for e in edu if e.get("major")]
    if majors:
        return majors, "application (major)"
    degrees = [e["degree"] for e in edu if e.get("degree") and re.search(r"\bin\b", e["degree"], re.I)]
    if degrees:
        return degrees, "application (degree)"
    if app.get("background"):
        return [app["background"]], "application (field of study)"
    degree = undergrad_degree(unified)
    if degree:
        return [field_of(degree)], "packet (degree line)"
    text = (sources.get("scores") or {}).get("text") or ""
    found = [m.group(1).strip() for m in _MAJOR_LINE.finditer(text)][:2]
    if found:
        return found, "packet (transcript)"
    return [], ""


_FIELD_PAREN = re.compile(r"[(\[]\s*([A-Za-z][^)\]]{2,80})[)\]]")
_FIELD_IN = re.compile(r"\bin\s+([A-Z][^,;()]*?)\s*(?:,|;|\bwith\b|\bminor\b|$)")


def field_of(degree: str) -> str:
    """"B.Tech (Electrical Engineering)" -> "Electrical Engineering";
    "Bachelor of Science in Photonics" -> "Photonics". The whole line when
    neither shape fits, so nothing is lost."""
    m = _FIELD_PAREN.search(degree or "")
    if m:
        return m.group(1).strip()
    m = _FIELD_IN.search((degree or "").strip(" .,;"))
    if m:
        return m.group(1).strip()
    return (degree or "").strip()


def ece_check(unified: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    majors, source = applicant_majors(unified)
    if not majors:
        return {"status": "unknown", "majors": [], "source": ""}
    ok = any(_ece_match(m, rules) for m in majors)
    return {"status": "ece" if ok else "other", "majors": majors, "source": source}


def _is_tamu(name: str) -> bool:
    low = (name or "").lower()
    return bool(re.search(r"texas a\s*&\s*m|texas a and m|\btamu\b", low)) and \
        not re.search(r"commerce|corpus|kingsville|international|san antonio|"
                      r"texarkana|central texas|prairie view|tarleton|west texas", low)


def gpa_check(unified: Dict[str, Any], track: str, threshold: float) -> Dict[str, Any]:
    """Which GPA the 3.25 rule is applied to, and whether it passes.

    1. CAS's cumulative undergraduate GPA when CAS calculated one (it's on a
       4.0 scale by construction).
    2. Otherwise the per-school GPAs on a 4.0 scale; for Quick Admit the Texas
       A&M one if it's there, since that's whose department the rule is about.
    3. If they all clear the bar it passes, if none do it fails, and if the
       schools disagree a person picks which one applies. A GPA on another
       scale (9.14/10) is never converted to make the call: the conversion is a
       guess and the rule is stated on 4.0.
    """
    app = ((unified.get("sources") or {}).get("application") or {})
    cum = (app.get("gpa_summary") or {}).get("undergraduate")
    if cum:
        vals = [(float(cum), "CAS cumulative undergrad")]
        others: List[str] = []
    else:
        entries = [e for e in app.get("education") or [] if e.get("gpa")]
        if track == QUICK_ADMIT:
            tamu = [e for e in entries if _is_tamu(e.get("college_name", ""))]
            entries = tamu or entries
        four = [e for e in entries if abs(float(e["gpa"].get("scale", 0)) - 4.0) < 0.01]
        others = [f"{e['gpa']['raw']:g}/{e['gpa']['scale']:g} "
                  f"({e.get('college_name') or 'school'})"
                  for e in entries if e not in four]
        vals = [(float(e["gpa"]["raw"]), e.get("college_name") or "school") for e in four]
    if not vals:
        if others:
            return {"status": "scale", "values": others,
                    "text": "; ".join(others)}
        return {"status": "missing", "values": [], "text": ""}
    text = "; ".join(f"{v:g} ({src})" for v, src in vals)
    eps = 1e-9
    passing = [v for v, _ in vals if v + eps >= threshold]
    if len(passing) == len(vals):
        status = "pass"
    elif not passing:
        status = "fail"
    else:
        status = "mixed"
    return {"status": status, "values": [v for v, _ in vals], "text": text,
            "other_scales": others}


_MASTERS = re.compile(r"(?<![a-z])(master(?:'?s)?\b|m\.\s?s\b|m\.\s?sc\b|msc\b|m\.\s?eng\b|"
                      r"meng\b|m\.\s?tech\b|mtech\b|m\.\s?e\.)", re.I)
_MS_BARE = re.compile(r"\bMS\b(?!\s*(?i:office|word|excel|powerpoint|access|project|teams|"
                      r"sql|visio|windows|dos|dynamics|azure))")
_BACHELOR = re.compile(r"(?<![a-z])(bachelor|b\.\s?s\b|b\.\s?sc\b|bsc\b|b\.\s?e\.|b\.\s?eng\b|"
                       r"b\.\s?tech\b|btech\b|b\.\s?a\.)", re.I)
_IN_PROGRESS = re.compile(r"expected|pursuing|present|current|candidate|in progress|"
                          r"anticipated|ongoing|to be awarded", re.I)


def masters_check(unified: Dict[str, Any]) -> Dict[str, Any]:
    """Does the applicant already hold a master's? True / False / None
    (couldn't tell). An MS in progress doesn't count as held."""
    sources = unified.get("sources") or {}
    app = sources.get("application") or {}
    degrees = [e.get("degree") for e in app.get("education") or [] if e.get("degree")]
    if degrees:
        held = [d for d in degrees if _MASTERS.search(d) or _MS_BARE.search(d)]
        if held:
            return {"has_ms": True, "evidence": held[0], "source": "application"}
        if any(_BACHELOR.search(d) for d in degrees):
            return {"has_ms": False, "evidence": "; ".join(degrees), "source": "application"}
    lines: List[str] = []
    resume = sources.get("resume") or {}
    prof = ((resume.get("analysis") or {}).get("profile") or {})
    lines += list(prof.get("education") or [])
    for key in ("resume", "scores"):
        lines += str((sources.get(key) or {}).get("text") or "").splitlines()
    in_progress = None
    bachelor = None
    for line in lines:
        line = _WS.sub(" ", line).strip()
        if not line or len(line) > 220:
            continue
        if _MASTERS.search(line) or _MS_BARE.search(line):
            if _IN_PROGRESS.search(line):
                in_progress = in_progress or line
                continue
            return {"has_ms": True, "evidence": _clip(line), "source": "packet"}
        if bachelor is None and _BACHELOR.search(line):
            bachelor = line
    if in_progress:
        return {"has_ms": None, "evidence": _clip(in_progress),
                "source": "packet", "note": "master's looks in progress"}
    if bachelor:
        return {"has_ms": False, "evidence": _clip(bachelor), "source": "packet"}
    return {"has_ms": None, "evidence": "", "source": ""}


# ------------------------------------------------------------ the verdict --

def _gpa_reason(g: Dict[str, Any], threshold: float) -> Optional[str]:
    st = g["status"]
    if st == "pass":
        return None
    if st == "fail":
        return f"GPA {g['text']} is below {threshold:g}"
    if st == "mixed":
        return (f"GPAs on file straddle {threshold:g} ({g['text']}); "
                "confirm which one applies")
    if st == "scale":
        return (f"GPA isn't on a 4.0 scale ({g['text']}); the {threshold:g} rule "
                "needs a 4.0-scale GPA")
    return "no GPA on file"


def _ece_reason(e: Dict[str, Any]) -> Optional[str]:
    if e["status"] == "ece":
        return None
    if e["status"] == "other":
        return f"not from ECE (major: {_clip('; '.join(e['majors']), 90)})"
    return "couldn't find the applicant's major/department"


def _courses_reason(c: Dict[str, Any], source: str) -> List[str]:
    if not c["listed"]:
        return ["no course list found in the application"]
    return [f"courses: {p}" for p in c["problems"]]


def _letter_reason(label: str, found: Dict[str, Any]) -> Optional[str]:
    if found["status"] == "exact":
        return None
    if found["status"] == "close":
        return (f"{label} found ({found['where']}) but the wording isn't exact "
                f"({int(found['coverage'] * 100)}% match); compare it to the required text")
    return f"{label} not found"


def _letter_text(found: Dict[str, Any]) -> str:
    if found["status"] == "exact":
        return f"Exact wording ({found['where']})"
    if found["status"] == "close":
        return f"Close, not exact ({found['where']}, {int(found['coverage'] * 100)}%)"
    return "Not found"


def evaluate(unified: Dict[str, Any], rules: Optional[Dict[str, Any]] = None,
             approved: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The prescreen result for one applicant. Pure: reads the record, returns
    a dict, changes nothing."""
    rules = rules if rules is not None else load_rules()
    approved = approved if approved is not None else load_courses()
    sources = unified.get("sources") or {}
    app = sources.get("application") or {}
    pages = unified.get("packet_evidence") or []
    name = (unified.get("identity") or {}).get("full_name") or ""

    cls = classify(app.get("programs") or [], rules)
    track = cls["track"]
    result: Dict[str, Any] = {
        "track": track, "track_label": TRACK_LABELS[track],
        "program": cls["program"], "thesis": cls["thesis"],
        "outcome": "", "reasons": [], "verify": [], "checks": {},
        "rules_version": rules_version(),
    }
    if cls.get("others"):
        result["verify"].append("also applied to: " + "; ".join(cls["others"]))
    if track not in SCREENED:
        return result

    threshold = float(rules.get("gpa_threshold", 3.25))
    ccfg = rules.get("courses") or {}
    lcfg = rules.get("letters") or {}
    prefixes = ccfg.get("prefixes") or ["ECEN", "ECE", "ELEN", "CSCE"]
    reasons: List[str] = []
    verify: List[str] = result["verify"]
    checks = result["checks"]

    if track in (QUICK_ADMIT, MS_DISTANCE):
        ece = ece_check(unified, rules)
        gpa = gpa_check(unified, track, threshold)
        checks["ece"], checks["gpa"] = ece, gpa
        for r in (_ece_reason(ece), _gpa_reason(gpa, threshold)):
            if r:
                reasons.append(r)
        if ece["status"] == "ece" and ece["source"].startswith("packet"):
            verify.append(f"major read from the {ece['source']}: {_clip(ece['majors'][0], 80)}")
        if gpa.get("other_scales"):
            verify.append("other GPAs on file: " + "; ".join(gpa["other_scales"]))

    if track in (MS_DISTANCE, PHD_DISTANCE):
        listed, where = find_course_list(app, pages, prefixes)
        if track == MS_DISTANCE:
            required = int(ccfg.get("ms_required", 10))
            ms = None
        else:
            ms = masters_check(unified)
            checks["masters"] = ms
            with_ms = int(ccfg.get("phd_required_with_ms", 6))
            bach = int(ccfg.get("phd_required_bachelors", 14))
            required = with_ms if ms["has_ms"] else bach if ms["has_ms"] is False else with_ms
        courses = check_courses(listed, required, approved, ccfg)
        courses["source"] = where
        checks["courses"] = courses
        if not courses["ok"]:
            reasons += _courses_reason(courses, where)
        if track == PHD_DISTANCE and ms and ms["has_ms"] is None:
            bach = int(ccfg.get("phd_required_bachelors", 14))
            if not courses["ok"] or courses["counted"] < bach:
                reasons.append(f"couldn't confirm whether the applicant holds an MS "
                               f"({required} courses needed with an MS, {bach} with a "
                               "Bachelor's only)")
        if courses["not_on_list"]:
            verify.append("not on the approved distance list: " + ", ".join(courses["not_on_list"]))
        if courses["below_400"]:
            verify.append("below 400-level, not counted: " + ", ".join(courses["below_400"]))
        if where.startswith("packet"):
            verify.append(f"course list read from the {where}; confirm it's the degree plan")

    if track == MS_DISTANCE:
        if cls["thesis"] is None:
            reasons.append("program name doesn't say thesis or non-thesis")
        if cls["thesis"]:
            adv = find_advisor_proof(pages)
            checks["advisor"] = adv
            if adv["status"] != "found":
                reasons.append("no document confirming a faculty research advisor")
            else:
                verify.append(f"advisor confirmation ({adv['where']}): confirm it's from "
                              "TAMU faculty and signed")
            employed = match_letter(pages, lcfg.get("ms_employed", ""), lcfg, name)
            unemployed = match_letter(pages, lcfg.get("ms_unemployed", ""), lcfg, name)
            best = employed if employed["status"] == "exact" else \
                unemployed if unemployed["status"] == "exact" else \
                max((employed, unemployed), key=lambda f: f.get("coverage", 0))
            best = dict(best, option="A (employed)" if best is employed else "B (unemployed)")
            checks["employer_letter"] = best
            r = _letter_reason("employer letter / unemployment statement", best)
            if r:
                reasons.append(r)
            elif best["option"].startswith("A"):
                verify.append("employer letter: confirm company letterhead and signature")
            else:
                verify.append("unemployment statement: confirm it's signed")
            if best.get("name_mismatch"):
                verify.append(f"letter names '{best['name_mismatch']}', not the applicant")

    if track == PHD_DISTANCE:
        letter = match_letter(pages, lcfg.get("phd_employed", ""), lcfg, name)
        checks["employer_letter"] = dict(letter, option="PhD employer letter")
        r = _letter_reason("PhD employer letter", letter)
        if r:
            reasons.append(r)
        else:
            verify.append("employer letter: confirm company letterhead and signature")
        if letter.get("name_mismatch"):
            verify.append(f"letter names '{letter['name_mismatch']}', not the applicant")

    result["reasons"] = reasons
    result["outcome"] = REVIEW if reasons else ADMIT
    return result


# ---------------------------------------------------------- summary facts --

_AWARD_HEAD = re.compile(
    r"^(honou?rs?|awards?|achievements?|scholarships?|accomplishments|recognitions?|"
    r"distinctions?)((\s*(and|&|,)\s*)(honou?rs?|awards?|achievements?|scholarships?|"
    r"recognitions?|distinctions?))*\s*:?$", re.I)
_RESUME_HEAD = re.compile(
    r"^(education|(work |professional |research |industry )?experience|(technical )?skills|"
    r"projects?|academic projects|publications?|certifications?|activities|leadership|"
    r"extra[- ]?curricular\w*|interests|hobbies|references|languages|(relevant )?coursework|"
    r"volunteer\w*|summary|objective|profile|positions? of responsibility)\s*:?$", re.I)
# Fallback when a résumé has no Awards/Honors heading: lines that say an
# award was won. Narrower than "mentions award", because transcripts and
# degree certificates bound into the résumé section say "award of this
# degree" and "with Honours" without anyone having won anything.
_AWARD_CUE = re.compile(r"\b(won|winner|received|recipient|awarded|secured|bagged|"
                        r"scholarships?|fellowships?|dean'?s list|medal(ist)?|prize|"
                        r"(1st|2nd|3rd|first|second|third) place|ranked|"
                        r"graduated with (high(est)? )?honou?rs?|cum laude)\b", re.I)
_NOT_AWARD = re.compile(r"\b(degree|regulations?|certif\w*|transcript|semester|marks|"
                        r"grade|credits?|examination)\b", re.I)


def awards_from_text(text: str, limit: int = 6) -> List[str]:
    """Lines under an Awards/Honors heading in a résumé; failing that, lines
    that name an award. Quoted (clipped), never reworded."""
    out: List[str] = []
    inside = False
    for raw in (text or "").splitlines():
        line = _clip(raw, 400)
        if not line:
            continue
        if len(line) <= 60 and _AWARD_HEAD.match(line):
            inside = True
            continue
        if inside:
            if len(line) <= 60 and _RESUME_HEAD.match(line):
                inside = False
                continue
            out.append(_clip(line, 160))
            if len(out) >= limit:
                break
    if out:
        return out
    for raw in (text or "").splitlines():
        line = _clip(raw, 400)
        if 3 <= len(line.split()) <= 30 and _AWARD_CUE.search(line) \
                and not _NOT_AWARD.search(line):
            clipped = _clip(line, 160)
            if clipped not in out:
                out.append(clipped)
            if len(out) >= min(limit, 4):
                break
    return out


def _fmt(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:g}"


def profile(unified: Dict[str, Any]) -> Dict[str, Any]:
    """The fields the grad office asked to see for every applicant, as display
    strings: schools with GPA, degree type, major, GRE, English proficiency,
    awards, domestic/international. Roster first; packet fills gaps, marked."""
    from .review import undergrad_degree
    sources = unified.get("sources") or {}
    app = sources.get("application") or {}
    scores = (((sources.get("scores") or {}).get("analysis") or {}).get("test_scores") or {})

    schools = []
    for e in app.get("education") or []:
        bits = [e.get("college_name") or "(school not named)"]
        detail = ", ".join(x for x in (e.get("degree"), e.get("major")) if x)
        if detail:
            bits[0] += f" ({detail})"
        g = e.get("gpa")
        if g:
            bits.append(f"GPA {_fmt(g['raw'])}/{_fmt(g['scale'])}")
        schools.append(": ".join(bits))
    summ = app.get("gpa_summary") or {}
    if summ.get("undergraduate"):
        schools.append(f"CAS cumulative undergrad GPA {_fmt(summ['undergraduate'])}")
    if summ.get("graduate"):
        schools.append(f"CAS cumulative graduate GPA {_fmt(summ['graduate'])}")

    degree_types = [e["degree"] for e in app.get("education") or [] if e.get("degree")]
    packet_degree = undergrad_degree(unified)
    if not degree_types and packet_degree:
        degree_types = [f"{packet_degree} (from packet)"]
    majors, msrc = applicant_majors(unified)
    major = "; ".join(majors)
    if major and msrc.startswith("packet"):
        major += " (from packet)"

    gre = app.get("gre") or {}
    if gre:
        parts = [f"{k} {_fmt(gre[key])}" for k, key in (("V", "verbal"), ("Q", "quant"),
                                                         ("AW", "writing")) if gre.get(key)]
        gre_text = " ".join(parts) + ("" if gre.get("official") else " (self-reported)")
    elif app.get("gre_results"):
        gre_text = ", ".join(_fmt(v) for v in app["gre_results"])
    elif scores.get("gre_verbal") or scores.get("gre_quant"):
        parts = [f"{k} {_fmt(scores[key])}" for k, key in (("V", "gre_verbal"), ("Q", "gre_quant"),
                                                           ("AW", "gre_awa")) if scores.get(key)]
        gre_text = " ".join(parts) + " (parsed from packet)"
    else:
        gre_text = ""

    eng = app.get("english_proficiency") or {}
    english = []
    if eng.get("best_toefl_total"):
        english.append(f"TOEFL {_fmt(eng['best_toefl_total'])}")
    if eng.get("best_ielts_overall"):
        english.append(f"IELTS {_fmt(eng['best_ielts_overall'])}")
    if eng.get("best_duolingo"):
        english.append(f"Duolingo {_fmt(eng['best_duolingo'])}")
    if eng.get("best_pte"):
        english.append(f"PTE {_fmt(eng['best_pte'])}")
    if eng.get("elpe"):
        english.append(f"ELPE {eng['elpe']}")
    if not eng.get("best_toefl_total") and scores.get("toefl_total"):
        english.append(f"TOEFL {_fmt(scores['toefl_total'])} (parsed from packet)")
    if not eng.get("best_ielts_overall") and scores.get("ielts_overall"):
        english.append(f"IELTS {_fmt(scores['ielts_overall'])} (parsed from packet)")

    awards = list(app.get("awards") or [])
    for line in awards_from_text((sources.get("resume") or {}).get("text") or ""):
        if line not in awards:
            awards.append(line)

    return {
        "residency": app.get("residency") or "",
        "schools": schools,
        "degree_type": "; ".join(degree_types),
        "major": major,
        "gre": gre_text,
        "english": "; ".join(english),
        "awards": awards[:6],
    }
