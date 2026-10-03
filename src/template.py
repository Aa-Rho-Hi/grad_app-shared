"""Stage 5 — deterministic descriptive summary. No LLM, no scoring.

Renders a *scannable card* per applicant: a three-line header (identity, the
numbers, the program applied to), then short headed sections — Background,
Experience, Projects, Notable, In their words, Letters, On file, Verify. Every
line is derived from data already present; nothing is invented or judged.

Design note: an earlier version packed each section onto one `- Label: a; b; c`
line, which turned a résumé's four experience bullets into a semicolon-joined
paragraph nobody could skim. Sections now carry one item per line, so a reviewer
working through hundreds of applicants can find a section by its heading and
read its items as a list. Each item still carries enough text to state a full
claim rather than a fragment — see EXCERPT/QUOTE below. Source sentences are
clipped, never paraphrased, and the full document text stays in the record for
anyone who wants it.

Output contract (parsed by templates/_summary.html, and valid Markdown so the
.md download renders as a document — `##` there is the applicant name, so
sections sit at `###`):

    <name> · <email>                     <- block 1, line 1
    GPA … · GRE … · 3 LOR(s), avg …/5    <- block 1, line 2
    Applied: <program> — <status>        <- block 1, line 3 (optional)

    ### Background                       <- one block per section
    - <item>
    - <Label>: <item>
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from .analyze import motivation_sentences
from .screening import awards_from_text

# Clip budgets (characters). Long enough to carry a full claim from the source
# sentence — a résumé bullet that stops at "in order to obtain better" has told
# the reviewer nothing. The full document text is always in the record too, so
# these can be raised again without losing anything but scannability.
EXCERPT = 320
QUOTE = 460

# A resume "education" line worth showing states a credential. Anything else in
# that block is objective statements, header junk, or GPA (already in the facts
# strip), so it is dropped rather than clipped.
# "B.E." must keep its dots: the undotted form matched the word "be", so an
# objective statement ("...and be a part of a renowned university") passed as a
# credential and landed in Background.
_DEGREE_RE = re.compile(
    r"\b(bachelor|master|doctor|b\.?s\.?c?|m\.?s\.?c?|b\.?tech|m\.?tech|b\.e\.?|"
    r"b\.?eng|m\.?eng|ph\.?\s?d|diploma|major(?:ed)? in)\b", re.I)


def _name(rec: dict) -> str:
    p = rec.get("personal", {})
    return p.get("full_name") or rec.get("cas_id", "The applicant")


def _best_gpa(rec: dict):
    gpas = [(e.get("college_name", "an institution"), e["gpa"])
            for e in rec.get("education", []) if "gpa" in e]
    return max(gpas, key=lambda x: x[1]["normalized_4"]) if gpas else None


def _doc_areas(rec: dict) -> List[str]:
    docs = rec.get("_documents") or {}
    areas = set()
    for key in ("sop", "resume"):
        d = docs.get(key)
        if d and d.get("analysis"):
            areas.update(d["analysis"].get("detected_areas") or [])
    return sorted(areas)


def _doc_skills(rec: dict) -> List[str]:
    docs = rec.get("_documents") or {}
    skills = set()
    for key in ("sop", "resume"):
        d = docs.get(key)
        if d and d.get("analysis"):
            skills.update(d["analysis"].get("mentioned_skills") or [])
    return sorted(skills)


def _analysis(doc: dict | None) -> dict:
    return (doc or {}).get("analysis") or {}


def _profile(doc: dict | None) -> dict:
    return _analysis(doc).get("profile") or {}


def _clip(text: Any, limit: int) -> str:
    """Collapse whitespace and cut to `limit` chars on a word boundary."""
    s = " ".join(str(text or "").split()).rstrip(" .…")
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def _items(values, count: int, limit: int = EXCERPT) -> List[str]:
    """Clip the first `count` non-empty entries of a profile list."""
    out = []
    for v in (values or []):
        c = _clip(v, limit)
        if c and c not in out:
            out.append(c)
        if len(out) >= count:
            break
    return out


def _bullets(items: List[str]) -> List[str]:
    """One item per line — the whole point of the section layout."""
    return [f"- {i}" for i in items if i]


def _labeled(label: str, items: List[str], sep: str = ", ") -> List[str]:
    """A single bullet whose items read as one phrase (areas, skills, files)."""
    return [f"- {label}: " + sep.join(items)] if items else []


def _grouped(label: str, items: List[str]) -> List[str]:
    """Label the first item, then let the rest stand as their own bullets.

    Repeating "Fit:" on every line reads like three separate findings; labeling
    once and continuing the list keeps them visibly part of one answer.
    """
    if not items:
        return []
    return [f"- {label}: {items[0]}"] + [f"- {i}" for i in items[1:]]


def _section(title: str, lines: List[str]) -> str:
    """A heading plus its lines, or nothing at all if the section is empty."""
    lines = [ln for ln in lines if ln]
    return "\n".join([f"### {title}"] + lines) if lines else ""


def _gpa_bit(rec: dict, resume_gpa_lines: List[str]) -> str:
    best = _best_gpa(rec)
    if best:
        g = best[1]
        # Show the number the spreadsheet actually holds. The /4.0 conversion
        # is only as good as the scale, and when the export never stated one
        # the scale was inferred from the magnitude — printing "3.8/4.0" for a
        # value that might be out of 5 states a fact the data doesn't support.
        assumed = any("GPA scale was auto-detected" in w
                      for w in (rec.get("meta", {}).get("warnings") or []))
        if assumed:
            return f"GPA {g['raw']:g} (scale not stated in the sheet)"
        if abs(g["scale"] - 4.0) < 0.01:
            return f"GPA {g['raw']:g}/4.0"
        return f"GPA {g['raw']:g}/{g['scale']:g} (≈{g['normalized_4']}/4.0)"
    if resume_gpa_lines:
        return "GPA per resume only — " + _clip(resume_gpa_lines[0], 70)
    return "no GPA on file"


def _test_bits(rec: dict, docs: dict) -> tuple[List[str], bool]:
    """Structured test scores first; packet-parsed figures fill gaps, marked
    "(parsed)" so the reviewer verifies them against official records."""
    eng = rec.get("english_proficiency", {})
    bits: List[str] = []
    if "best_ielts_overall" in eng:
        bits.append(f"IELTS {eng['best_ielts_overall']}")
    if "best_toefl_total" in eng:
        bits.append(f"TOEFL {eng['best_toefl_total']:.0f}")
    if "best_duolingo" in eng:
        bits.append(f"Duolingo {eng['best_duolingo']:.0f}")
    if "best_pte" in eng:
        bits.append(f"PTE {eng['best_pte']:.0f}")
    if eng.get("elpe"):
        bits.append(f"ELPE {eng['elpe']}")
    gre = rec.get("gre") or {}
    if gre:
        sec = " ".join(f"{k}{gre[key]:g}" for k, key in (("V", "verbal"), ("Q", "quant"),
                                                         ("AW ", "writing")) if gre.get(key))
        bits.append(f"GRE {sec}" + ("" if gre.get("official") else " (self-reported)"))
    elif rec.get("gre_results"):
        vals = ", ".join(str(int(v)) if float(v).is_integer() else str(v)
                         for v in rec["gre_results"])
        ok = any(260 <= float(v) <= 340 for v in rec["gre_results"])
        bits.append(f"GRE {vals}" if ok else f"GRE {vals} (⚠ not a valid 260–340 total)")

    ts = _analysis(docs.get("scores")).get("test_scores") or {}
    parsed = False
    if "best_ielts_overall" not in eng and "ielts_overall" in ts:
        bits.append(f"IELTS {ts['ielts_overall']} (parsed)"); parsed = True
    if "best_toefl_total" not in eng and "toefl_total" in ts:
        bits.append(f"TOEFL {ts['toefl_total']} (parsed)"); parsed = True
    if not gre and not rec.get("gre_results") and ("gre_verbal" in ts or "gre_quant" in ts):
        sub = []
        if "gre_verbal" in ts:
            sub.append(f"V{ts['gre_verbal']}")
        if "gre_quant" in ts:
            sub.append(f"Q{ts['gre_quant']}")
        if "gre_awa" in ts:
            sub.append(f"AWA {ts['gre_awa']}")
        head = f"GRE {ts['gre_total']}" if "gre_total" in ts else "GRE"
        bits.append(f"{head} ({'/'.join(sub)}, parsed)"); parsed = True
    return bits, parsed


def _roster(lors_all: List[dict]) -> List[str]:
    """Every named recommender: Likert reviewers ∪ letter-only writers."""
    roster = next((l.get("recommenders") for l in lors_all if l.get("recommenders")), None)
    if not roster:
        evaluators = next((l.get("likert", {}).get("evaluators")
                           for l in lors_all if l.get("likert")), None) or []
        roster = [e.get("evaluator") for e in evaluators if e.get("evaluator")]
    if not roster:
        roster = [l.get("recommender") for l in lors_all if l.get("recommender")]
    return list(roster or [])


def render(rec: Dict[str, Any]) -> str:
    name = _name(rec)
    docs = rec.get("_documents") or {}
    resume, sop = docs.get("resume"), docs.get("sop")
    lors_all = docs.get("lors") or []
    resume_prof = _profile(resume)
    resume_lines = [ln for vals in resume_prof.values() if isinstance(vals, list) for ln in vals]
    resume_gpa_lines = [ln for ln in resume_lines if "gpa" in ln.lower()]

    roster = _roster(lors_all)
    likert = next((l.get("likert") for l in lors_all if l.get("likert")), None)
    n_lor = len(roster) or len(lors_all)

    # ---- Facts strip: the numbers a reviewer scans for ----
    facts = [_gpa_bit(rec, resume_gpa_lines)]
    tests, parsed_used = _test_bits(rec, docs)
    if tests:
        facts.append(", ".join(tests))
    elif resume_test_lines := resume_prof.get("test_scores", []):
        facts.append("tests per resume only — " + _clip(resume_test_lines[0], 70))
    else:
        facts.append("no test scores on file")
    if n_lor:
        lor_bit = f"{n_lor} LOR(s)"
        if likert and likert.get("overall_average") is not None:
            lor_bit += f", avg {likert['overall_average']}/5"
        facts.append(lor_bit)
    email = rec.get("personal", {}).get("email")

    # ---- Header: who, the numbers, the program. One idea per line. ----
    head: List[str] = [" · ".join([name] + ([email] if email else [])
                                  + ([rec["residency"]] if rec.get("residency") else []))]
    if facts:
        head.append(" · ".join(facts))
    progs = rec.get("programs", [])
    if progs:
        line = f"Applied: {progs[0].get('name', 'n/a')}"
        if len(progs) > 1:
            line += f" (+{len(progs) - 1} more)"
        if progs[0].get("status"):
            line += f" — status: {progs[0]['status']}"
        head.append(line)
    paras: List[str] = ["\n".join(head)]

    # ---- Background: where they studied, in what, on what topics ----
    # Structured institutions are authoritative; the resume fills in the degree.
    insts = [e.get("college_name") for e in rec.get("education", []) if e.get("college_name")]
    seen = re.sub(r"\W+", "", " ".join(insts)).lower()
    # When the export carries each school's degree and major, say so beside
    # the school; that's the line the grad office reads first.
    insts = [e["college_name"] + (f" ({', '.join(x for x in (e.get('degree'), e.get('major')) if x)})"
                                  if e.get("degree") or e.get("major") else "")
             for e in rec.get("education", []) if e.get("college_name")]
    degrees = [d for d in _items(
        [e for e in resume_prof.get("education", []) if _DEGREE_RE.search(str(e))], 3, 140)
        if re.sub(r"\W+", "", d).lower() not in seen]
    bg: List[str] = []
    if insts:
        bg.append(" — ".join([", ".join(insts[:3])] + degrees))
    else:
        bg += degrees

    stated = sorted(set((rec.get("interests", {}).get("specialization") or [])
                        + (rec.get("interests", {}).get("areas") or [])))
    detected = [d for d in _doc_areas(rec) if d not in stated]
    areas = list(stated)
    if detected:
        areas.append(("also " if stated else "") + ", ".join(detected[:8])
                     + " (keyword-detected)")
    skills = _doc_skills(rec)
    # A résumé's own "Skills: Python, Matlab…" line lands in technical
    # preparation, and the detected-skills line above already says it — printing
    # both put the same list on screen twice. Keep the preparation items that
    # add something (software, certifications, coursework).
    prep = [p for p in _items(resume_prof.get("technical_preparation", []), 4)
            if not re.match(r"^\s*skills?\b\s*[:\-–]", p, re.I)]
    field = [rec["background"]] if rec.get("background") else []
    background = (_bullets(bg) + _labeled("Field", field) + _labeled("Areas", areas, "; ")
                  + _labeled("Skills", skills[:16]) + _bullets(prep[:3]))

    exp = _items(resume_prof.get("experience", []), 6)
    if not exp and resume:
        exp = ["resume captured, but no confident experience lines extracted"]
    projects = _items(resume_prof.get("projects", []), 4)
    # Awards first (the roster's own award fields, then the résumé's Awards /
    # Honors block), then the résumé's other distinctions.
    award_lines = list(rec.get("awards") or [])
    for line in awards_from_text((resume or {}).get("text") or ""):
        if line not in award_lines:
            award_lines.append(line)
    awards = _items(award_lines, 4)
    seen_awards = {re.sub(r"\W+", "", a).lower()[:60] for a in awards}
    notable = [d for d in _items(resume_prof.get("distinctions", []), 4)
               if re.sub(r"\W+", "", d).lower()[:60] not in seen_awards]

    # ---- In their words: quoted, never interpreted. The old "SOP detail" and
    # "Motivation" blocks overlapped heavily, so the quoted goal and the parsed
    # fit/direction lines are kept distinct here instead. ----
    words: List[str] = []
    if sop and sop.get("text"):
        sop_prof = _profile(sop)
        quotes = motivation_sentences(sop["text"], k=3) or sop_prof.get("goals") or []
        if quotes:
            words += _grouped("Goal", [f"“{_clip(q, QUOTE)}”" for q in quotes[:3]])
        else:
            words.append("- Goal: SOP on file; no explicit goal sentence detected")
        words += _grouped("Fit", _items(sop_prof.get("program_fit", []), 3))
        words += _grouped("Direction", _items(sop_prof.get("career_direction", []), 3))

    letters: List[str] = []
    if roster or lors_all:
        who = ", ".join(roster) if roster else "recommender names not parsed"
        recs = sorted({e.get("recommendation") for e in (likert or {}).get("evaluators", [])
                       if e.get("recommendation")})
        if recs:
            shown = "; ".join(f"“{r[:1].upper() + r[1:]}”" for r in recs)
            who += " — " + ("all " if len(recs) == 1 and n_lor > 1 else "") + shown
        evidence: List[str] = []
        for lor in lors_all:
            evidence += _profile(lor).get("evidence", [])
        letters = _bullets([who] + [f"“{e}”" for e in _items(evidence, 5)])

    # ---- Documents on file ----
    doc_bits = []
    if resume:
        doc_bits.append(f"resume {resume.get('word_count', 0)}w")
    if sop:
        doc_bits.append(f"SOP {sop.get('word_count', 0)}w")
    # LOR count is already in the header; repeating the page-group count here
    # only invited "3 LORs vs 1 LOR" confusion.
    if docs.get("scores"):
        doc_bits.append(f"scores/transcript {docs['scores'].get('word_count', 0)}w")
    ocr = rec.get("_ocr") or {}
    if ocr.get("ocr_pages_read"):
        doc_bits.append(f"{ocr['ocr_pages_read']} page(s) read by OCR — minor errors possible")

    # ---- Data to verify (incl. missing materials) ----
    warns = list(rec.get("meta", {}).get("warnings") or [])
    missing = [lbl for lbl, present in
               (("resume", resume), ("SOP", sop), ("LOR", lors_all)) if not present]
    if missing:
        warns.append(" and ".join(missing) + " missing or not identified.")
    if parsed_used:
        warns.append("Some scores parsed from the packet — check official records.")

    paras += [_section("Background", background),
              _section("Experience", _bullets(exp)),
              _section("Projects", _bullets(projects)),
              _section("Awards", _bullets(awards)),
              _section("Notable", _bullets(notable)),
              _section("In their words (SOP)", words),
              _section("Letters", letters),
              _section("On file", _bullets([" · ".join(doc_bits)])),
              _section("Verify", _bullets([_clip(w, 200) for w in warns]))]
    return "\n\n".join(p for p in paras if p)
