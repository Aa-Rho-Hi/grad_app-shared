"""Segment a combined "Full Application" PDF into sections — local, no LLM.

Admissions exports bundle each student's resume, statement of purpose, letters,
score cards, transcripts — and sometimes an entire thesis — into ONE PDF. We
label every page with the section it belongs to, then keep resume / SOP / LOR /
scores and drop forms, boilerplate and thesis/appendix material.

Approach (a small, deterministic sequence labeler — no LLM, no network):

  1. **Per-page emission scores.** Each page is scored *independently* for every
     candidate label (resume, sop, lor, scores, ignore) from a bundle of
     features: heading phrases, a contact block, first-person prose density,
     LOR/reference form fields, score-report & transcript cues, and
     form/thesis/math detectors. No page inherits a label just because of its
     neighbour.
  2. **Viterbi decode.** The page labels are decoded jointly with transition
     penalties, so sections come out as contiguous blocks and a stray page can't
     flip the surrounding section — but a score card or form *can* interrupt a
     run cheaply. This replaces the old "carry the previous label forward" rule,
     which dumped theses and form pages into whatever section preceded them.

It still reports what it found and flags low confidence rather than pretending to
be exact.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .analyze import load_keywords

# leading numeric token in the filename is the cas_id, e.g.
# "1000419138_Joonha_Jun_Full Application_200948_Fall 2020 ...pdf"
_CAS_FROM_NAME = re.compile(r"^\s*(\d{4,})")


def cas_id_from_filename(filename: str) -> Optional[str]:
    m = _CAS_FROM_NAME.match(os.path.basename(filename))
    return m.group(1) if m else None


def _titlecase_name(token: str) -> str:
    """Title-case a name token, preserving internal hyphens (CHEN-NI -> Chen-Ni)."""
    return "-".join(p.capitalize() for p in token.split("-")) if token else token


def name_from_filename(filename: str) -> Dict[str, str]:
    """Best-effort applicant name from the file name. Handles both real CAS export
    styles after the leading cas_id:
        '<cas_id>_<First>_<Last>_Full Application_...'   (underscores)
        '<cas_id> <First> <Last> - Full Application ...' (spaces, ' - ' separator)
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _CAS_FROM_NAME.match(stem)
    if not m:
        return {}
    rest = stem[m.end():].strip(" _-")

    if "_" in rest and " " not in rest.split("_")[0]:
        tokens = [t for t in rest.split("_") if t]
    else:
        # space style: name is everything before ' - ' or 'Full Application'
        head = re.split(r"\s+-\s+|\bfull application\b", rest, maxsplit=1,
                        flags=re.IGNORECASE)[0]
        tokens = head.split()

    # drop trailing boilerplate tokens
    clean = []
    for t in tokens:
        tl = t.lower()
        if tl.startswith("full") or "application" in tl or tl in ("pdf", "utc"):
            break
        clean.append(t.strip(","))
    clean = [t for t in clean if t]
    if not clean:
        return {}

    # "LIU, CHEN-NI" -> reorder; otherwise First ... Last
    if clean[0].endswith(","):
        clean[0] = clean[0].rstrip(",")
    parts = [_titlecase_name(t) for t in clean[:4]]
    out: Dict[str, str] = {"first_name": parts[0]}
    if len(parts) > 1:
        out["last_name"] = parts[-1]
    out["full_name"] = " ".join(parts)
    return out


def _read_bytes(source) -> bytes:
    if hasattr(source, "read"):
        data = source.read()
        try:
            source.seek(0)
        except Exception:
            pass
        return data
    with open(source, "rb") as f:
        return f.read()


def extract_pages(source) -> List[str]:
    """Return a list of page texts (one per page), OCR'ing scanned pages."""
    from . import ocr
    pages, _ = ocr.pdf_page_texts(_read_bytes(source))
    return pages


_EMAIL = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")
_PHONE = re.compile(r"(?:\+?\d[\d\-\s().]{7,}\d)")


def _looks_like_contact(page_text: str) -> bool:
    top = "\n".join(page_text.splitlines()[:6])
    return bool(_EMAIL.search(top) and _PHONE.search(top))


# Labels the decoder can assign. 'ignore' absorbs forms, boilerplate and
# thesis/appendix pages (everything we deliberately do not keep).
LABELS = ("resume", "sop", "lor", "scores", "ignore")

# Transition penalties for the Viterbi decode. Switching between the three
# "document" sections is discouraged (they are contiguous blocks); interrupting
# a run with a score card or a form is cheap (those can appear anywhere).
_SWITCH_DOC = 1.6        # resume <-> sop <-> lor
_SWITCH_INTERRUPT = 0.4  # to/from scores or ignore

# Multi-word first-person phrases — a far more reliable SOP signal than a bare
# "I", which appears scattered through garbled OCR (e.g. "E I ectr i ca I").
_SOP_FP = re.compile(
    r"\b(i am|i have|i was|i will|i would|i decided|i believe|i came|i want|"
    r"i wish|i hope|i intend|i aim|i plan|i aspire|my research|my goal|"
    r"my interest|my passion|my desire|my motivation|my career|my future)\b")
# A recommendation LETTER is first-person too, so the tell is a salutation/closing
# plus talking ABOUT the applicant (third person + honorifics) and recommend verbs.
_LOR_LETTER = ("dear admission", "dear members of", "to whom it may concern",
               "recommendation letter", "letter of recommendation",
               "letter of reference", "i highly recommend", "i strongly recommend",
               "it is my pleasure to recommend", "i am pleased to recommend",
               "yours sincerely", "yours truly", "respectfully submitted")
_THIRD_PERSON = re.compile(r"\b(he|his|him|she|her|hers|mr\.|ms\.|mrs\.)\b")
# Bare first-person pronouns — a strong SOP signal, but only trustworthy on
# coherent prose (garbled OCR scatters lone "i"s), so gated on a coherence check.
_FIRST_PERSON_BARE = re.compile(r"\b(i|my|me|myself|i'm|i've)\b")
# SOP vocabulary — catches essay-style statements that open with a third-person
# hook and don't use "I am/my goal" phrasing up front.
_SOP_VOCAB = ("statement of purpose", "personal statement", "graduate program",
              "graduate school", "graduate studies", "master's", "ph.d", "phd",
              "doctoral", "research interest", "pursue", "admission", "faculty",
              "thesis", "career goal", "my passion", "i am applying", "motivat",
              "aspire", "fascinat", "specialization", "research experience")
_RESUME_TERMS = ("education", "experience", "skills", "projects", "curriculum vitae",
                 "work experience", "professional experience", "internship",
                 "relevant coursework", "publications", "achievements", "objective")
_LOR_TERMS = ("letter of reference", "letter of recommendation", "likert", "i recommend",
              "i strongly recommend", "it is my pleasure", "references", "professional title",
              "waiver of evaluation", "permission to contact", "response due date",
              "recommender", "evaluator", "relationship to the applicant", "how long have you")
_SCORE_TERMS = ("test taker score report", "score report", "scaled score", "percentile",
                "toefl", "ielts", "band score", "graduate record", "verbal reasoning",
                "quantitative reasoning", "analytical writing", "test date", "test center")
_TRANSCRIPT_TERMS = ("transcript", "marks obtained", "grade point", "cgpa", "sgpa",
                     "semester", "scanned by camscanner", "subject code", "roll no",
                     "registration no", "examination", "certificate of", "percentage",
                     "academic records", "degree conferred", "credits grade",
                     "date of degree", "credit hours")
_FORM_TERMS = ("application for admission", "academic history", "supporting information",
               "designations", "document requested", "release statement", "program level",
               "start term", "submitted date", "verified date", "application status",
               "by accepting these terms", "documents")
_THESIS_TERMS = ("declaration of authorship", "table of contents", "list of figures",
                 "list of tables", "acknowledgements", "bibliography", "abstract\n",
                 "thesis titled", "undergraduate thesis", "this thesis",
                 "master's thesis", "doctoral thesis", "dissertation")
_MATH_CHARS = ("∑", "∫", "≥", "≤", "α", "β", "∈", "θ", "λ", "∀", "√", "∂", "∇")
# CAS pulls its form headings out of the text layer with spaces sprinkled
# through them ("RELEA SE S TATEMENT", "By accepting these t erms"), so the
# plain matches in _FORM_TERMS and section_headers.ignore never fire on those
# pages. These are matched against a de-spaced copy of the page head instead.
# Deliberately narrow: de-spacing "academic history" / "supporting information"
# drops the GRE and GPA-by-transcript pages out of `scores`, and "designations"
# pulls the evaluator/Likert form pages out of `lor`.
_CAS_BOILERPLATE = ("releasestatement", "byacceptingtheseterms",
                    "biographicinformation", "customquestions")
_DESPACE = re.compile(r"[^a-z0-9]")


def _count(low: str, terms) -> int:
    return sum(1 for t in terms if t in low)


def _page_scores(text: str, headers: Dict[str, List[str]]) -> Dict[str, float]:
    """Independent emission score per label for a single page. Higher = more
    likely. 'ignore' carries a small baseline so an unrecognized page is dropped
    rather than bleeding into an adjacent section."""
    low = text.lower()
    lines = text.splitlines()
    head = "\n".join(lines[:8]).lower()
    nwords = len(low.split())
    sc = {l: 0.0 for l in LABELS}
    sc["ignore"] = 0.5

    # explicit heading at the top of the page (strong signal)
    for sect, phrases in (headers or {}).items():
        target = "scores" if sect == "scorecard" else sect
        if target not in LABELS:
            target = "ignore"
        if any(p.strip().lower() in head for p in phrases):
            sc[target] += 3.0

    # resume: contact block + resume section words + bullet density
    if _looks_like_contact(text):
        sc["resume"] += 2.0
    sc["resume"] += min(2.0, 0.5 * _count(low, _RESUME_TERMS))
    bullets = sum(1 for ln in lines if ln.strip()[:1] in "•-*∙·▪◦")
    if bullets >= 4:
        sc["resume"] += 1.0

    # sop: multi-word first-person phrases, plus first-person pronoun DENSITY on
    # coherent prose (handles essay-style SOPs that open with a third-person hook),
    # plus SOP vocabulary. The coherence gate keeps garbled OCR from scoring as SOP.
    if nwords >= 80:
        sc["sop"] += min(3.2, 0.6 * len(_SOP_FP.findall(low)))
        toks = low.split()
        coherent = (sum(1 for w in toks if len(w) >= 3 and w.isalpha())
                    / max(1, len(toks))) > 0.5
        if coherent:
            sc["sop"] += min(2.5, 0.08 * len(_FIRST_PERSON_BARE.findall(low)))
        sc["sop"] += min(2.0, 0.4 * _count(low, _SOP_VOCAB))
    if any(k in head for k in ("statement of purpose", "personal statement",
                               "statement of intent", "research statement")):
        sc["sop"] += 2.0

    # lor: reference/recommendation FORM fields, plus narrative-LETTER detection
    # (salutation/closing + recommend verbs + talking about the applicant in the
    # third person) so a recommendation letter isn't mistaken for an SOP/resume.
    sc["lor"] += min(3.0, 0.9 * _count(low, _LOR_TERMS))
    sc["lor"] += min(3.0, 1.3 * _count(low, _LOR_LETTER))
    # Sustained third-person narrative (he/his/him + honorifics) is the signature
    # of a letter ABOUT someone — true even on a body/closing page that carries no
    # salutation or "recommend" verb. SOPs are first-person about self (low here);
    # resumes are telegraphic (low here); transcripts have none.
    third = len(_THIRD_PERSON.findall(low))
    if nwords >= 120 and third >= 8:
        sc["lor"] += min(2.2, 0.12 * third)
    # a letter closing next to an academic/professional title = a signature page
    if any(c in low for c in ("sincerely", "yours faithfully", "respectfully", "regards")) \
            and any(t in low for t in ("professor", "ph.d", "department", "university", "lecturer")):
        sc["lor"] += 2.5

    # scores: standardized-test reports + academic transcripts
    sc["scores"] += min(3.0, 0.8 * _count(low, _SCORE_TERMS))
    sc["scores"] += min(2.6, 0.7 * _count(low, _TRANSCRIPT_TERMS))

    # ignore: application forms, boilerplate, and thesis/appendix material.
    # Thesis markers are weighted strongly: thesis prose (e.g. an Acknowledgements
    # page) is first-person and would otherwise be mistaken for an SOP.
    sc["ignore"] += min(3.0, 0.8 * _count(low, _FORM_TERMS))
    sc["ignore"] += min(3.2, 1.6 * _count(low, _THESIS_TERMS))
    # CAS boilerplate whose heading the text layer garbles. Without this a
    # release statement scores as an SOP on its first-person prose ("I certify",
    # "I have read"), and on a packet with no real SOP (Quick Admit admissions
    # carry none) it becomes the whole sop section AND suppresses the
    # "could not locate a statement-of-purpose section" flag.
    if any(t in _DESPACE.sub("", "\n".join(lines[:12]).lower())
           for t in _CAS_BOILERPLATE):
        sc["ignore"] += 3.0
    if sum(low.count(ch) for ch in _MATH_CHARS) >= 3:
        sc["ignore"] += 1.6
    if re.search(r"\bchapter\s+\d", low) or re.search(r"\bfigure\s+\d+\.\d", low):
        sc["ignore"] += 1.6
    return sc


def _transition(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a in ("scores", "ignore") or b in ("scores", "ignore"):
        return _SWITCH_INTERRUPT
    return _SWITCH_DOC


def _viterbi(emissions: List[Dict[str, float]]) -> List[str]:
    """Decode the most likely label sequence: maximize Σ emission − Σ transition."""
    if not emissions:
        return []
    dp = [{l: emissions[0].get(l, 0.0) for l in LABELS}]
    back: List[Dict[str, str]] = [{}]
    for i in range(1, len(emissions)):
        row, brow = {}, {}
        for l in LABELS:
            best_prev, best_val = None, None
            for pl in LABELS:
                val = dp[i - 1][pl] - _transition(pl, l)
                if best_val is None or val > best_val:
                    best_val, best_prev = val, pl
            row[l] = emissions[i].get(l, 0.0) + best_val
            brow[l] = best_prev
        dp.append(row)
        back.append(brow)
    last = max(LABELS, key=lambda l: dp[-1][l])
    seq = [last]
    for i in range(len(emissions) - 1, 0, -1):
        last = back[i][last]
        seq.append(last)
    return seq[::-1]


def segment(path: str, kw: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Segment a combined PDF. Returns {sections, order, page_count, flags, ocr}.

    Each page is scored independently across all labels, then the page sequence
    is Viterbi-decoded so sections are contiguous and no page inherits a label
    purely from its neighbour."""
    from . import ocr
    kw = kw or load_keywords()
    headers = kw.get("section_headers", {}) or {}
    pages, ocr_info = ocr.pdf_page_texts(_read_bytes(path))

    emissions = [_page_scores(t, headers) for t in pages]
    labels = _viterbi(emissions)

    sections: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for i, (text, label) in enumerate(zip(pages, labels)):
        if label == "ignore":
            continue
        if not order or order[-1] != label:
            order.append(label)
        s = sections.setdefault(label, {"text": "", "pages": []})
        s["text"] += ("\n" if s["text"] else "") + text
        s["pages"].append(i + 1)
    for s in sections.values():
        s["text"] = s["text"].strip()
        s["page_range"] = [s["pages"][0], s["pages"][-1]] if s["pages"] else []

    flags: List[str] = []
    if ocr_info.get("ocr_used"):
        flags.append(f"OCR read {ocr_info['ocr_used']} scanned page(s).")
    if ocr_info.get("ocr_unavailable"):
        flags.append(f"{ocr_info['scanned_pages']} page(s) appear scanned but OCR is not "
                     f"installed — text from those pages is missing (install tesseract).")
    if "sop" not in sections:
        flags.append("Could not locate a statement-of-purpose section in the packet.")
    if "resume" not in sections:
        flags.append("Could not locate a resume/CV section in the packet.")
    # non-contiguous section = a page in the middle was labelled something else;
    # surface it so a reviewer can sanity-check the split.
    for label, s in sections.items():
        span = s["pages"]
        if span and (span[-1] - span[0] + 1) != len(span):
            flags.append(f"The {label} section spans non-adjacent pages "
                         f"({', '.join(map(str, span))}) — verify the split.")

    # Every page's text and label too: the prescreen rules look for course
    # lists and letters on pages the section split deliberately drops
    # (forms, "ignore"), so they need the raw pages, not just the sections.
    return {"sections": sections, "order": order, "page_count": len(pages),
            "flags": flags, "ocr": ocr_info, "pages": pages, "labels": labels}
