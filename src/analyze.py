"""Deterministic (no-LLM) text analysis for SOPs/LORs.

Pure local computation: keyword detection, length/structure flags, and
near-duplicate detection. Nothing is sent anywhere; no model is involved.
"""
from __future__ import annotations

import os
import re
import zlib
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_KW = os.path.join(ROOT, "config", "keywords.yaml")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")


@lru_cache(maxsize=4)
def load_keywords(path: str = _DEFAULT_KW) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _term_present(text_lower: str, term: str) -> bool:
    """Word-boundary keyword match (text already lower-cased).

    Substring matching over-fires badly — "soc" hits "associated", "git" hits
    "digital", "nlp"/"dsp" hit random tokens — so short/acronym/symbol tokens
    require boundaries on *both* sides. Ordinary words and multi-word phrases
    anchor on a leading boundary but allow a trailing suffix so simple plurals
    still match ("power system" -> "power systems", "photonic" -> "photonics").
    """
    t = (term or "").lower().strip()
    if not t:
        return False
    esc = re.escape(t)
    compact = t.replace(" ", "")
    if " " not in t and (len(t) <= 4 or not compact.isalpha()):
        pat = rf"(?<![a-z0-9]){esc}(?![a-z0-9])"      # strict: acronym/short/symbol
    else:
        pat = rf"(?<![a-z0-9]){esc}"                  # word/phrase: allow plural suffix
    return re.search(pat, text_lower) is not None


def _detect(text_lower: str, terms: List[str]) -> List[str]:
    return [t for t in terms if _term_present(text_lower, t)]


def _tokens(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def _shingles(text: str, n: int = 3) -> set:
    toks = _tokens(text)
    return {" ".join(toks[i:i + n]) for i in range(max(0, len(toks) - n + 1))}


# A bottom-k MinHash sketch of an SOP's trigrams. Small enough to keep on every
# index entry, and comparable without loading the document — which is what
# turns "compare this SOP to every other one" from a full-table read into a
# few hundred integer set operations.
_SKETCH_SIZE = 96


def sketch(text: str) -> List[int]:
    """Stable, deterministic sketch. `hash()` would be wrong here: it is salted
    per process, so a sketch written today wouldn't match one read tomorrow."""
    hashes = sorted(zlib.crc32(sh.encode("utf-8")) for sh in _shingles(text))
    return hashes[:_SKETCH_SIZE]


def sketch_similarity(a: Sequence[int], b: Sequence[int]) -> float:
    """Estimated Jaccard from two sketches (bottom-k MinHash).

    An estimate, deliberately: it is used to *shortlist* candidates, and the
    pair that survives is then compared exactly. Cheap and slightly wrong is
    the right trade for a filter; it would be the wrong trade for the number a
    reviewer sees.
    """
    if not a or not b:
        return 0.0
    k = min(len(a), len(b))
    both, union = set(a) & set(b), sorted(set(a) | set(b))[:k]
    return round(sum(1 for h in union if h in both) / k, 3) if union else 0.0


def shingles(text: str):
    """The trigram set behind `jaccard`, exposed so a caller comparing one text
    against many can tokenize each of them once instead of once per pair."""
    return _shingles(text)


def jaccard_sets(sa, sb) -> float:
    if not sa or not sb:
        return 0.0
    union = len(sa | sb)
    return round(len(sa & sb) / union, 3) if union else 0.0


def jaccard(a: str, b: str) -> float:
    """Trigram Jaccard similarity of two texts (0..1). Deterministic."""
    return jaccard_sets(_shingles(a), _shingles(b))


_MOTIVATION_CUES = [
    "i want", "i aim", "i aspire", "i hope to", "i intend", "i wish to", "i am eager",
    "my goal", "my motivation", "my aspiration", "my long-term", "my career",
    "i am passionate", "i am motivated", "i am driven", "driven by", "passionate about",
    "i am interested in", "my research interest", "i plan to", "i would like to",
    "pursue a", "i decided to", "inspired me", "fascinated by", "i am excited",
]

_RESUME_EDU_CUES = (
    "education", "university", "college", "bachelor", "master", "degree",
    "gpa", "graduated", "major", "minor", "relevant course", "coursework",
)
_RESUME_SKILL_CUES = (
    "skill", "software", "programming", "language", "tools", "certification",
    "simulation", "matlab", "python", "c++", "verilog", "vhdl", "spice",
    "ansys", "cad", "pcb",
)
_RESUME_EXPERIENCE_CUES = (
    "experience", "research", "intern", "laboratory", "lab", "work",
    "teacher", "assistant", "volunteer", "performed",
    "participated", "optimized", "constructed", "developed", "designed",
    "simulated", "programmed", "implemented", "managed", "led",
)
_RESUME_PROJECT_CUES = (
    "project", "course project", "capstone", "built", "designed", "developed",
    "simulated", "constructed", "programmed", "implemented", "model",
)
_RESUME_DISTINCTION_CUES = (
    "award", "honor", "scholarship", "publication", "published", "won",
    "rank", "selected", "leadership",
)
_RESUME_TEST_CUES = ("gre", "toefl", "ielts", "test score")

_SOP_PREP_CUES = (
    "experience", "worked", "project", "lab", "laboratory", "course",
    "coursework", "research", "designed", "developed", "built", "simulated",
    "manufacturing", "internship", "undergraduate", "learned", "studied",
)
_SOP_FIT_CUES = (
    "texas a&m", "tamu", "program", "department", "university", "faculty",
    "professor", "curriculum", "lab", "research group",
)
_SOP_CAREER_CUES = (
    "career", "long-term", "future", "industry", "workforce",
    "after graduation", "aspire to", "professional",
)
_LOR_EVIDENCE_CUES = (
    "recommend", "applicant", "student", "known", "worked", "course",
    "research", "project", "ability", "excellent", "outstanding", "strong",
    "exceptional", "performance", "rank", "potential", "independent",
)


# --- OCR run-together repair ("degreeon" -> "degree on") -------------------
# Conservative by construction: a token is only split when a *closed set* of
# short function words is glued to the end AND the left part is a real word
# (verified against an optional system dictionary plus a small curated list).
# A real word that is itself recognised is never touched, so "information",
# "understand", "behavior", "thousand" etc. pass through unchanged.
_GLUE_TAIL = ("from", "with", "that", "this", "and", "for", "the",
              "on", "of", "in", "to", "as", "at", "by", "is", "an", "or")

_GLUE_STEMS = frozenset("""
degree degrees regulation regulations university education engineering research
semester undergraduate graduate bachelor master masters science technology
department program programs project projects experience knowledge scholarship
award awards internship transmission electrical electronics power system systems
control signal communication communications design analysis software hardware
course courses study studies student students faculty professor institute
institution application statement purpose recommendation reference school college
year years field work future career industry skills ability performance
examination board admission semiconductor laboratory
""".split())

_GLUE_TOKEN = re.compile(r"[A-Za-z]{8,}")


@lru_cache(maxsize=1)
def _known_words() -> frozenset:
    words: set = set(_GLUE_STEMS)
    for p in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                words.update(w.strip().lower() for w in f
                             if w.strip().isalpha() and len(w.strip()) > 2)
        except OSError:
            continue
    return frozenset(words)


def _deglue(s: str) -> str:
    known = _known_words()

    def fix(m: "re.Match") -> str:
        tok = m.group(0)
        low = tok.lower()
        if low in known:                       # a real word -> never split
            return tok
        for tail in _GLUE_TAIL:
            if not low.endswith(tail):
                continue
            cut = len(low) - len(tail)
            if cut < 4:
                continue
            left = low[:cut]
            if left in known or (left.endswith("s") and left[:-1] in known):
                return tok[:cut] + " " + tok[cut:]   # keep original casing
        return tok

    return _GLUE_TOKEN.sub(fix, s)


def _clean_piece(s: str, max_chars: int = 220) -> str:
    s = re.sub(r"[\u2022\u2023\u25e6\u2043\u2219\uf000-\uf8ff]", " ", s)
    # OCR cleanup: a "|" is usually a mis-read "I" or a leftover table border.
    s = re.sub(r"^\|\s+(?=[a-z])", "I ", s)            # leading "| have"  -> "I have"
    s = re.sub(r"(?<=\s)\|(?=\s+[a-z])", "I", s)       # mid-line "| was" -> "I was"
    s = s.replace("|", " ")                            # any remaining pipes = borders
    s = re.sub(r"(?i)^applicant:\s*.*?\bpersonal statement\b\s*", "", s)
    s = re.sub(r"(?i)^m\.?s\.?\s+in\s+.*?\bpersonal statement\b\s*", "", s)
    # CAS running header bled into mid-sentence: "Applicant: LAST, FIRST MS in
    # <program> of <Institution> University". Anchored to an ALL-CAPS-style name
    # + a degree token + an institution word so it can't match ordinary prose
    # such as "the applicant: a dedicated student from State University".
    s = re.sub(
        r"(?i)\bApplicant:\s*[A-Z][A-Za-z,.\-]*(?:\s+[A-Z][A-Za-z,.\-]*){0,4}\s+"
        r"(?:M\.?S\.?|M\.?Eng|Ph\.?D\.?|B\.?S\.?|B\.?Tech|M\.?Tech)\b.*?"
        r"\b(?:University|Institute|College)\b\s*",
        " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—:;,\t")
    s = _deglue(s)                                          # repair OCR run-together words
    s = re.sub(r"^\d{1,3}(?=\s+[A-Za-z])", "", s).strip()   # drop a leading page number
    if len(s) > max_chars:
        head = s[:max_chars]
        # Prefer to end on a sentence/clause boundary rather than mid-word.
        cut = max(head.rfind(". "), head.rfind("; "), head.rfind(", "))
        if cut < int(max_chars * 0.6):                # boundary too early -> last word
            cut = head.rfind(" ")
        s = (head[:cut].rstrip(" .,;:") if cut > 0 else head.rstrip()) + "…"
    return s


# CAS form scaffolding, page footers and Likert-form questions that surround the
# real narrative in a packet. Any line/sentence containing one of these is noise.
_NOISE_MARKERS = (
    "generated:", "applicant id", "engineeringcas", "application status",
    "texas a&m university fall", "type: letter of reference", "designations",
    "supplemental questions", "reference ratings", "recommendation concerning",
    "how long have you", "how well do you", "in what capacity",
    "the applicant has waived", "waiver of evaluation", "permission to contact",
    "date completed", "response due date", "request date", "occupation:",
    "professional title:", "organization:", "telephone:", "document requested",
    "uploaded file name", "release statement", "by accepting these terms",
    "academic history", "supporting information", "biographic information",
    "program level", "start term", "submitted date", "verified date",
    "last updated", "not observed", "evaluator information", "status: completed",
    "date of birth", "citizenship status", "race/ethnicity", "native language",
)


def _is_noise(line: str) -> bool:
    """True if a line/sentence is CAS form/footer scaffolding, not real narrative."""
    low = line.lower().strip()
    if not low:
        return True
    if any(m in low for m in _NOISE_MARKERS):
        return True
    if sum(c.isalpha() for c in low) < 3:        # symbol/number-only garble
        return True
    return False


def _stitch_wrapped(text: str) -> List[str]:
    """Rejoin a sentence/bullet that the PDF wrapped across physical lines.

    Resume text arrives line-by-line, so a single bullet that the page wrapped
    becomes two physical lines and the first ends mid-thought ("...to maintain
    low and"). If a line doesn't end on terminal/clause punctuation and the next
    line starts lower-case (a continuation, not a new bullet — those start with a
    capitalised action verb), fold the next line onto it.
    """
    merged: List[str] = []
    for raw in text.splitlines():
        cur = raw.strip()
        if not cur:
            continue
        if (merged
                and not re.search(r"[.!?:;)]$", merged[-1])
                and re.match(r"[a-z(]", cur)):
            merged[-1] = merged[-1] + " " + cur
        else:
            merged.append(cur)
    return merged


def _clean_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in _stitch_wrapped(text):
        line = _clean_piece(raw)
        if not line or _is_noise(line):
            continue
        if len(line.split()) == 1 and len(line) < 18:
            continue
        out.append(line)
    return out


def _dedupe_keep_order(items: List[str], limit: int = 4) -> List[str]:
    seen, out = set(), []
    for item in items:
        item = _clean_piece(item)
        if not item:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", item.lower()).strip()[:90]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _has_cue(low: str, cue: str) -> bool:
    cue = cue.lower()
    if cue in {"gre", "toefl", "ielts", "gpa"} or (cue.isalpha() and len(cue) <= 4):
        return bool(re.search(rf"(?<![a-z]){re.escape(cue)}(?![a-z])", low))
    return cue in low


def _lines_matching(lines: List[str], cues, *, limit: int = 4,
                    min_words: int = 3) -> List[str]:
    hits = []
    for line in lines:
        low = line.lower()
        if len(line.split()) < min_words:
            continue
        if any(_has_cue(low, c) for c in cues):
            hits.append(line)
    return _dedupe_keep_order(hits, limit)


def _sentences_matching(text: str, cues, *, limit: int = 3,
                        min_words: int = 8, max_words: int = 60) -> List[str]:
    hits = []
    for sent in _split_sentences(text):
        clean = _clean_piece(sent, max_chars=400)
        n = len(clean.split())
        if n < min_words or n > max_words:
            continue
        if _is_noise(clean):
            continue
        low = clean.lower()
        if any(_has_cue(low, c) for c in cues):
            hits.append(clean)
    return _dedupe_keep_order(hits, limit)


def _resume_profile(text: str, analysis: Dict[str, Any]) -> Dict[str, List[str]]:
    lines = _clean_lines(text)
    skills = analysis.get("mentioned_skills") or []
    profile = {
        "education": _lines_matching(lines, _RESUME_EDU_CUES, limit=4),
        "test_scores": _lines_matching(lines, _RESUME_TEST_CUES, limit=3, min_words=1),
        "technical_preparation": _lines_matching(lines, _RESUME_SKILL_CUES, limit=4),
        "experience": _lines_matching(lines, _RESUME_EXPERIENCE_CUES, limit=5),
        "projects": _lines_matching(lines, _RESUME_PROJECT_CUES, limit=4),
        "distinctions": _lines_matching(lines, _RESUME_DISTINCTION_CUES, limit=3),
    }
    if skills and not profile["technical_preparation"]:
        profile["technical_preparation"] = [f"Tools/skills detected: {', '.join(skills)}"]

    def _key(ln: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", ln.lower()).strip()[:80]

    def _dedupe(cat: str, against: set) -> None:
        kept = []
        for ln in profile.get(cat, []):
            k = _key(ln)
            if not k or k in against:
                continue
            against.add(k)
            kept.append(ln)
        profile[cat] = kept

    # Education / distinctions / test scores claim their lines first.
    base: set = set()
    for cat in ("education", "test_scores", "distinctions"):
        _dedupe(cat, base)
    # Experience and projects share action-verb cues, so the same line often
    # matches both. De-dupe each against the base, then route any line that
    # landed in BOTH to exactly one bucket — projects when it actually names a
    # "project"/"capstone"/"thesis", experience otherwise — so nothing prints
    # twice.
    exp_seen, proj_seen = set(base), set(base)
    _dedupe("experience", exp_seen)
    _dedupe("projects", proj_seen)

    proj_keys = {_key(ln) for ln in profile["projects"]}
    exp_keys = {_key(ln) for ln in profile["experience"]}
    shared = proj_keys & exp_keys
    if shared:
        def _names_project(ln: str) -> bool:
            return bool(re.search(r"\b(project|capstone|thesis)\b", ln.lower()))
        profile["experience"] = [ln for ln in profile["experience"]
                                 if _key(ln) not in shared or not _names_project(ln)]
        profile["projects"] = [ln for ln in profile["projects"]
                               if _key(ln) not in shared or _names_project(ln)]

    # Technical preparation must not merely echo lines already shown elsewhere.
    _dedupe("technical_preparation", base | exp_seen | proj_seen)
    return profile


def _sop_profile(text: str) -> Dict[str, List[str]]:
    goals = motivation_sentences(text, k=4)
    profile = {
        "goals": _dedupe_keep_order([_clean_piece(g, 400) for g in goals], 4),
        "preparation": _sentences_matching(text, _SOP_PREP_CUES, limit=4),
        "program_fit": _sentences_matching(text, _SOP_FIT_CUES, limit=3),
        "career_direction": _sentences_matching(text, _SOP_CAREER_CUES, limit=3),
    }
    # a sentence should appear in one bucket only (the same line often matches
    # goal, preparation and career cues at once).
    seen: set = set()
    for cat in ("goals", "program_fit", "preparation", "career_direction"):
        kept = []
        for ln in profile.get(cat, []):
            key = re.sub(r"[^a-z0-9]+", " ", ln.lower()).strip()[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(ln)
        profile[cat] = kept
    return profile


_LOR_NARRATIVE = re.compile(
    r"\b(he|his|him|she|her|hers|mr\.|ms\.|mrs\.|the applicant|the student|"
    r"i recommend|i have known|i highly recommend|i strongly recommend|"
    r"i was impressed|i believe|i am pleased|it is my pleasure|i taught|"
    r"i have taught|i have been|i first met)\b")


def _lor_profile(text: str) -> Dict[str, List[str]]:
    """Pull the substantive sentences from a recommendation letter. A packet's
    LOR pages mix the actual letter with CAS evaluation forms, footers and the
    Likert questionnaire, so we keep only sentences that read like a letter
    *about the applicant* (third-person or recommend language) and drop the
    form/legal scaffolding."""
    evidence = []
    for sent in _split_sentences(text):
        clean = _clean_piece(sent, max_chars=300)
        n = len(clean.split())
        if n < 12 or n > 70:                       # full sentences, not fragments
            continue
        if _is_noise(clean):
            continue
        low = clean.lower()
        if any(skip in low for skip in (
            "occupational license", "eligibility requirements", "convicted",
            "texas occupations code", "local or county", "applicants should",
            "applicants are encouraged", "bacterial meningitis", "communication method",
        )):
            continue
        if not _LOR_NARRATIVE.search(low):         # must read like a letter
            continue
        evidence.append(clean)
    return {"evidence": _dedupe_keep_order(evidence, 4)}


# abbreviations that should NOT end a sentence (avoid splitting "M.S. EE", "Dr. X")
_ABBREV = ["mr", "mrs", "ms", "dr", "prof", "m.s", "b.s", "ph.d", "phd", "b.tech",
           "m.tech", "u.s", "u.k", "i.e", "e.g", "vs", "etc", "no", "inc", "st"]


def _split_sentences(text: str) -> list:
    text = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    # protect abbreviation dots so the splitter ignores them
    for ab in _ABBREV:
        text = re.sub(rf"(?i)\b{re.escape(ab)}\.", lambda m: m.group(0)[:-1] + "\x00", text)
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.replace("\x00", ".").strip() for p in parts if p.strip()]


def motivation_sentences(text: str, k: int = 3) -> list:
    """Deterministically pull the applicant's own goal/motivation sentences from
    an SOP. This QUOTES the applicant — it does not interpret or judge them."""
    if not text:
        return []
    sents = _split_sentences(text)
    scored = []
    for s in sents:
        s = s.strip()
        n = len(s.split())
        if n < 8 or n > 55:
            continue
        low = s.lower()
        hits = sum(1 for c in _MOTIVATION_CUES if c in low)
        if hits:
            scored.append((hits, s))
    seen, out = set(), []
    for _, s in sorted(scored, key=lambda x: -x[0]):
        key = s.lower()[:40]
        if key not in seen:
            seen.add(key); out.append(_clean_piece(s, 400))
        if len(out) >= k:
            break
    return out


def analyze_text(text: str, doc_type: str, kw: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return deterministic analysis of a document's text."""
    kw = kw or load_keywords()
    low = text.lower()
    words = _tokens(text)
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    wc = len(words)

    areas = {}
    for area, terms in (kw.get("research_areas") or {}).items():
        hits = _detect(low, terms)
        if hits:
            areas[area] = hits
    skills = _detect(low, kw.get("skills") or [])

    th = kw.get("thresholds", {})
    flags: List[str] = []
    if doc_type == "sop":
        if wc < th.get("sop_min_words", 150):
            flags.append(f"SOP is very short ({wc} words) — verify completeness.")
        elif wc > th.get("sop_max_words", 1500):
            flags.append(f"SOP is unusually long ({wc} words).")
        if not areas:
            flags.append("SOP does not clearly mention a recognized research area.")
    elif doc_type == "lor":
        if wc < th.get("lor_min_words", 100):
            flags.append(f"LOR is very short ({wc} words) — verify it is a full letter.")
    elif doc_type == "resume":
        if wc < th.get("resume_min_words", 80):
            flags.append(f"Resume text is very short ({wc} words) — may be image-only "
                         f"(scanned) or failed to extract.")

    result = {
        "word_count": wc,
        "sentence_count": len(sentences),
        "avg_sentence_words": round(wc / len(sentences), 1) if sentences else 0,
        "detected_areas": sorted(areas.keys()),
        "area_terms": areas,
        "mentioned_skills": sorted(set(skills)),
        "flags": flags,
    }
    if doc_type == "resume":
        result["profile"] = _resume_profile(text, result)
    elif doc_type == "sop":
        result["profile"] = _sop_profile(text)
    elif doc_type == "lor":
        result["profile"] = _lor_profile(text)
    return result


# --------------------------------------------------------------------------
# Structured extraction (deterministic, no LLM): recommender + test scores
# --------------------------------------------------------------------------

def _looks_like_name(s: str) -> bool:
    parts = s.split()
    if not (2 <= len(parts) <= 4):
        return False
    return all(p[:1].isupper() and p.replace(".", "").replace("-", "").isalpha()
               for p in parts)


def extract_recommender(text: str) -> Dict[str, Any]:
    """Pull the recommender's name (and title/organization when present) from an
    LOR. Handles the CAS 'REFERENCES' block, common signature closings, and a
    'Professor/Dr. <Name>' fallback. Returns {} if nothing confident is found.
    Deterministic; it reports what's written, it does not infer."""
    if not text:
        return {}
    lines = [ln.strip() for ln in text.splitlines()]
    out: Dict[str, Any] = {}

    # CAS export: a "REFERENCES" header followed by the recommender's name and,
    # below it, their title/organization. Scope title/org to this window so we
    # don't pick up the *target* program's "Organization:" line elsewhere.
    for i, ln in enumerate(lines):
        if ln.upper().startswith("REFERENCES"):
            window = lines[i + 1:i + 16]
            for cand in window:
                if cand and ":" not in cand and _looks_like_name(cand):
                    out["name"] = cand
                    break
            for w in window:
                m = re.match(r"(?:Professional Title|Title|Designation)\s*:\s*(.+)", w, re.I)
                if m and "title" not in out:
                    out["title"] = m.group(1).strip()
                m = re.match(r"Organization\s*:\s*(.+)", w, re.I)
                if m and "organization" not in out:
                    out["organization"] = m.group(1).strip()
            if "name" in out:
                break

    if "name" not in out:
        m = re.search(r"(?:Sincerely|Regards|Yours sincerely|Yours truly|"
                      r"Best regards|Respectfully)[,\s]*\n+\s*"
                      r"([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){1,3})", text)
        if m:
            out["name"] = m.group(1).strip()

    if "name" not in out:
        m = re.search(r"\b(?:Prof(?:essor)?\.?|Dr\.?)\s+"
                      r"([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,2})", text)
        if m and _looks_like_name(m.group(1)):
            out["name"] = m.group(1).strip()

    return out


_TITLE_PREFIX = re.compile(r"^(dr|prof|professor|mr|ms|mrs|miss)\.?\s+", re.I)
# role / suffix words that get appended to a name in headers and signatures
_ROLE_WORDS = {"professor", "associate", "assistant", "adjunct", "phd", "ph.d",
               "dr", "prof", "lecturer", "faculty", "department", "scientist",
               "engineer", "director", "dean", "chair", "emeritus", "msc", "msc.",
               "teaching", "occupation"}
# block headers that look name-like but are not people
_NOT_NAMES = {"evaluator information", "evaluations", "references", "reference ratings",
              "designations continued", "letter reference", "personal statement"}


def _clean_name(name: str) -> str:
    tokens = (name or "").replace("\n", " ").split()
    while tokens and tokens[0].lower().strip(".") in {"dr", "prof", "professor",
                                                      "mr", "ms", "mrs", "miss"}:
        tokens.pop(0)
    while tokens and tokens[-1].lower().strip(".,") in _ROLE_WORDS:
        tokens.pop()
    return " ".join(tokens).strip(" ,.")


def _name_tokens(name: str) -> set:
    return {t for t in re.sub(r"[^a-z\s-]", "", _clean_name(name).lower()).split()
            if len(t) > 1}


def _same_person(a: str, b: str) -> bool:
    """Fuzzy match: same person if their cleaned name tokens share the surname and
    a given name (≥2 common tokens), or one is a subset of the other."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    common = ta & tb
    return len(common) >= 2 or ta <= tb or tb <= ta


def extract_all_recommenders(text: str) -> List[str]:
    """Find every distinct recommender named in a combined LOR section — from the
    CAS evaluator/reference blocks, the top-of-page 'Type: Letter of Reference'
    header, and letter signature closings. Catches a reviewer who wrote a
    narrative letter without filling a Likert form. De-duplicated fuzzily."""
    if not text:
        return []
    found: List[str] = []

    def add(name: str) -> None:
        name = _clean_name(name)
        if not name or not _looks_like_name(name):
            return
        if _clean_name(name).lower() in _NOT_NAMES:
            return
        if not any(_same_person(name, f) for f in found):
            found.append(name)

    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        u = ln.upper()
        low = ln.lower()
        if "EVALUATOR INFORMATION" in u or u.startswith("REFERENCES") or "EVALUATIONS" in u:
            for cand in lines[i + 1:i + 4]:
                if cand and ":" not in cand and _looks_like_name(_clean_name(cand)):
                    add(cand)
                    break
        if "type: letter of reference" in low:
            head = re.split(r"type:\s*letter of reference", ln, flags=re.I)[0].strip()
            if _looks_like_name(_clean_name(head)):
                add(head)
            else:
                for cand in reversed(lines[max(0, i - 3):i]):
                    if cand and _looks_like_name(_clean_name(cand)):
                        add(cand)
                        break

    # signature closings (single line — do not cross into the next title line)
    for m in re.finditer(
            r"(?:Sincerely|Regards|Yours sincerely|Yours truly|Yours faithfully|"
            r"Best regards|Respectfully)[,\s]*\n+[ \t]*"
            r"((?:Dr\.?|Prof(?:essor)?\.?|Mr\.?|Ms\.?|Mrs\.?)?[ \t]*"
            r"[A-Z][A-Za-z.\-]+(?:[ \t]+[A-Z][A-Za-z.\-]+){1,3})", text):
        add(m.group(1))
    return found


def merge_recommenders(primary: List[str], extra: List[str]) -> List[str]:
    """Union two recommender name lists, keeping `primary` (e.g. the clean Likert
    roster) first and adding only `extra` names that aren't the same person."""
    roster = list(primary)
    for name in extra:
        if name and not any(_same_person(name, r) for r in roster):
            roster.append(name)
    return roster


def extract_test_scores(text: str) -> Dict[str, Any]:
    """Deterministically read standardized-test scores from a score report /
    transcript section (GRE, TOEFL, IELTS). Reports figures as written; it never
    fabricates a missing score."""
    if not text:
        return {}
    low = text.lower()
    out: Dict[str, Any] = {}

    m = re.search(r"verbal reasoning with your scaled score of\s*(\d{2,3})", low)
    if not m:
        m = re.search(r"verbal reasoning[^0-9]{0,40}?(\d{3})\b", low)
    if m and 130 <= int(m.group(1)) <= 170:
        out["gre_verbal"] = int(m.group(1))

    m = re.search(r"quantitative reasoning with your scaled score of\s*(\d{2,3})", low)
    if not m:
        m = re.search(r"quantitative reasoning[^0-9]{0,40}?(\d{3})\b", low)
    if m and 130 <= int(m.group(1)) <= 170:
        out["gre_quant"] = int(m.group(1))

    m = re.search(r"analytical writing[^0-9]{0,40}?([0-6](?:\.[05])?)\b", low)
    if m:
        out["gre_awa"] = float(m.group(1))

    if "gre_verbal" in out and "gre_quant" in out:
        out["gre_total"] = out["gre_verbal"] + out["gre_quant"]

    m = re.search(r"overall band score[:\s]*([0-9](?:\.[05])?)", low)
    if m:
        out["ielts_overall"] = float(m.group(1))

    m = re.search(r"toefl[^0-9]{0,80}?total[^0-9]{0,12}?(\d{1,3})", low, re.S)
    if not m:
        m = re.search(r"total score[:\s]*(\d{1,3})", low)
    if m and 0 <= int(m.group(1)) <= 120:
        out["toefl_total"] = int(m.group(1))

    return out


_DATE_RE = re.compile(r"\b\d{2}-\d{2}-\d{4}\b")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _row_numbers(line: str) -> List[float]:
    """Numbers on a data row, with the leading date and any 'NN%' percentile
    columns removed (CAS test tables interleave score and percentile)."""
    rest = _DATE_RE.sub(" ", line)
    rest = re.sub(r"\d+(?:\.\d+)?\s*%", " ", rest)        # drop percentiles
    return [float(n) for n in _NUM_RE.findall(rest)]


def extract_standardized_tests(text: str) -> Dict[str, Any]:
    """Parse the CAS 'STANDARDIZED TESTS' academic-history tables (GRE / IELTS /
    TOEFL) — a different layout from the ETS score reports handled above. Reads
    the value row under each test header. Reports figures as written.

        OFFICIAL GRE
        Date ETS Reg Code Verbal Quantitative Analytical Writing
        09-20-2019 148 37% 164 84% 3.5 39%      -> V 148, Q 164, AWA 3.5
        IELTS  ... 8.5 7.5 6.0 6.0 7.0           -> overall 7.0 (last band)
        TOEFL  ... 23 21 23 27 94                -> total 94 (last, <=120)
    """
    out: Dict[str, Any] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        u = line.upper()
        if "GRE" in u and len(line) < 40:
            current = "gre"
            continue
        if "IELTS" in u and len(line) < 60:
            current = "ielts"
            continue
        if "TOEFL" in u and len(line) < 60:
            current = "toefl"
            continue
        if not current or not _DATE_RE.search(line):
            continue
        nums = _row_numbers(line)
        if current == "gre":
            scaled = [n for n in nums if 130 <= n <= 170]
            awa = [n for n in nums if 0 < n <= 6 and (n * 2) == int(n * 2)]
            if len(scaled) >= 2:
                out.setdefault("gre_verbal", int(scaled[0]))
                out.setdefault("gre_quant", int(scaled[1]))
                if "gre_verbal" in out and "gre_quant" in out:
                    out["gre_total"] = out["gre_verbal"] + out["gre_quant"]
            if awa:
                out.setdefault("gre_awa", awa[0])
        elif current == "ielts":
            bands = [n for n in nums if 0 <= n <= 9]
            if bands:
                out.setdefault("ielts_overall", bands[-1])    # Overall Band is last
        elif current == "toefl":
            totals = [n for n in nums if 0 <= n <= 120]
            if totals:
                out.setdefault("toefl_total", int(totals[-1]))  # Total is last column
    return out
