"""Stage 7 — the swipe pass and the professor queues.

Where stage 6 (`review.py`) narrowed hundreds of applicants down to the ones
marked `tbd`, this is the second read: one card per applicant, swipe right to
shortlist and left to reject. What survives gets handed to a professor's queue.

Two fields, deliberately separate:

  `review_status`  accept / reject / tbd — the reviewer's spreadsheet column.
  `triage`         shortlist / reject   — what the swipe said.

A left swipe writes *both* (the applicant is rejected, and drops out of the tbd
pool). A right swipe only sets `triage`, leaving `review_status` at tbd, so a
shortlisted applicant stays in the pool they came from until a professor has
actually looked. `undo` puts both back, which is safe precisely because only
tbd applicants ever enter the deck.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFESSORS_YAML = os.path.join(ROOT, "config", "professors.yaml")

SHORTLIST, REJECT, UNDO = "shortlist", "reject", "undo"
SWIPES = (SHORTLIST, REJECT, UNDO)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# What a deck card shows, in order, and how many items of each.
#
# These were tight when the card had to fit one screen exactly; the body scrolls
# now, so the limits exist only to keep the *first* screen decisive rather than
# to hide anything. "On file" is still dropped — the header chips already say
# which documents exist — and everything is one click from the full record.
_CARD_SECTIONS = (
    ("Background", 5),
    ("Experience", 5),
    ("Projects", 3),
    ("Notable", 3),
    ("In their words (SOP)", 4),
    ("Letters", 4),
    ("Verify", 3),
)
_CARD_ITEM_CHARS = 260


def load_professors(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """The faculty list, in file order. Missing file -> no professors, not a
    crash: the rest of the app still works without the queue step."""
    try:
        with open(path or PROFESSORS_YAML, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        return []
    out = []
    for entry in data.get("professors") or []:
        if not entry.get("id") or not entry.get("name"):
            continue
        out.append({"id": str(entry["id"]), "name": str(entry["name"]),
                    "url": str(entry.get("url") or ""),
                    "areas": list(entry.get("areas") or [])})
    return out


def professor_by_id(prof_id: str, professors: Optional[Sequence[Dict]] = None):
    for p in (professors if professors is not None else load_professors()):
        if p["id"] == prof_id:
            return p
    return None


# ------------------------------------------------- editing the faculty list --

_YAML_HEADER = """\
# Faculty who can receive shortlisted applicants for review.
#
# Editable here or from the app's "Professor queues" page — both write this
# file. `id` is what gets stored on the applicant's record, so it is generated
# once from the name and then left alone: renaming a professor is safe, changing
# an id orphans whoever was already assigned.
#
# `areas` is optional and only used to help the reviewer pick.
"""

# Only these schemes ever reach an href. A `javascript:` or `data:` URL pasted
# into the profile field would otherwise execute when the link is clicked.
_SAFE_SCHEMES = ("http://", "https://")


class ProfessorError(ValueError):
    """A professor edit the user needs to fix (bad name, unsafe URL, …)."""


def slugify(name: str) -> str:
    """`Krishna Narayanan` -> `krishna-narayanan`. Ids are ASCII-ish and stable."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "professor"


def _unique_id(base: str, taken: Sequence[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def clean_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(_SAFE_SCHEMES):
        raise ProfessorError("A profile link must start with http:// or https://")
    return url


def _clean_areas(areas: Any) -> List[str]:
    if isinstance(areas, str):
        areas = areas.split(",")
    return [a.strip() for a in (areas or []) if str(a).strip()]


def add_professor(professors: List[Dict[str, Any]], name: str,
                  url: str = "", areas: Any = None) -> List[Dict[str, Any]]:
    name = str(name or "").strip()
    if not name:
        raise ProfessorError("A professor needs a name.")
    if any(p["name"].lower() == name.lower() for p in professors):
        raise ProfessorError(f"{name} is already on the list.")
    prof = {"id": _unique_id(slugify(name), [p["id"] for p in professors]),
            "name": name, "url": clean_url(url), "areas": _clean_areas(areas)}
    return list(professors) + [prof]


def update_professor(professors: List[Dict[str, Any]], prof_id: str, *,
                     name: Optional[str] = None, url: Optional[str] = None,
                     areas: Any = None) -> List[Dict[str, Any]]:
    """Change a professor's details. The id is never rewritten — assignments
    point at it, and a rename shouldn't empty someone's queue."""
    out, found = [], False
    for p in professors:
        if p["id"] != prof_id:
            out.append(p)
            continue
        found = True
        new_name = p["name"] if name is None else str(name).strip()
        if not new_name:
            raise ProfessorError("A professor needs a name.")
        out.append({"id": p["id"], "name": new_name,
                    "url": p["url"] if url is None else clean_url(url),
                    "areas": p["areas"] if areas is None else _clean_areas(areas)})
    if not found:
        raise ProfessorError("No such professor.")
    return out


def remove_professor(professors: List[Dict[str, Any]], prof_id: str) -> List[Dict[str, Any]]:
    out = [p for p in professors if p["id"] != prof_id]
    if len(out) == len(professors):
        raise ProfessorError("No such professor.")
    return out


def save_professors(professors: Sequence[Dict[str, Any]],
                    path: Optional[str] = None) -> str:
    """Write the list back, atomically — a half-written config would take the
    whole queue step down, and this file is edited by hand too."""
    path = path or PROFESSORS_YAML
    body = yaml.safe_dump(
        {"professors": [{"id": p["id"], "name": p["name"],
                         **({"url": p["url"]} if p.get("url") else {}),
                         **({"areas": list(p["areas"])} if p.get("areas") else {})}
                        for p in professors]},
        sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_YAML_HEADER + body)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------- the card ----

def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def condense_card(summary_text: str) -> str:
    """Trim a full summary card down to the deck version.

    Works on the rendered text rather than re-rendering from the record, so
    there is exactly one place that decides what a summary *says* — this only
    decides how much of it fits.
    """
    blocks = [b for b in (summary_text or "").split("\n\n") if b.strip()]
    if not blocks:
        return ""
    out = [blocks[0]]                       # header: identity, numbers, program
    wanted = dict(_CARD_SECTIONS)
    order = [name for name, _ in _CARD_SECTIONS]
    kept: Dict[str, str] = {}
    for block in blocks[1:]:
        lines = block.splitlines()
        title = lines[0][4:] if lines[0].startswith("### ") else ""
        if title not in wanted:
            continue
        items = [ln for ln in lines[1:] if ln.startswith("- ")][:wanted[title]]
        items = [f"- {_clip(i[2:], _CARD_ITEM_CHARS)}" for i in items]
        if items:
            kept[title] = "\n".join([f"### {title}"] + items)
    out += [kept[name] for name in order if name in kept]
    return "\n\n".join(out)


# ------------------------------------------------------------ the swipes ----

def deck(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Applicants still awaiting a swipe: marked tbd, not yet triaged."""
    return [e for e in entries
            if e.get("review_status") == "tbd" and not e.get("triage")]


def shortlisted(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in entries if e.get("triage") == SHORTLIST]


# The professor's own verdict on a student handed to them. Kept apart from
# `triage` (the advisor's swipe) and `review_status` (the spreadsheet) for the
# same reason those two are apart: three people can disagree, and flattening
# that into one field loses who said what.
PROF_FIELD = "professor_status"
PROF_ACCEPT, PROF_REJECT = "accept", "reject"
PROF_DECISIONS = (PROF_ACCEPT, PROF_REJECT, UNDO)


def assigned_to(entries: Sequence[Dict[str, Any]], prof_id: str) -> List[Dict[str, Any]]:
    """Everything in one professor's queue, decided or not."""
    return [e for e in entries if e.get("assigned_to") == prof_id]


def professor_deck(entries: Sequence[Dict[str, Any]], prof_id: str) -> List[Dict[str, Any]]:
    """Their queue, minus anyone they have already ruled on."""
    return [e for e in assigned_to(entries, prof_id) if not e.get(PROF_FIELD)]


def apply_professor_decision(unified: Dict[str, Any], decision: str) -> Dict[str, Any]:
    """Record a professor's accept/reject. `undo` clears it.

    Never touches `triage` or `review_status`: the advisor's shortlist stands
    whatever the professor decides, so the dashboard can show "advisor kept,
    professor passed" rather than one silently overwriting the other.
    """
    if decision == UNDO:
        unified.pop(PROF_FIELD, None)
        unified.pop("professor_decided_at", None)
    elif decision in (PROF_ACCEPT, PROF_REJECT):
        unified[PROF_FIELD] = decision
        unified["professor_decided_at"] = _now()
    else:
        raise ValueError(f"Unknown professor decision: {decision!r}")
    return unified


def decided(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Everyone you have swiped — shortlisted or rejected, in that order.

    This is what the summaries view and the export show. A card is the record of
    a read that happened; an applicant still in the deck hasn't been read.
    """
    order = {SHORTLIST: 0, REJECT: 1}
    return sorted((e for e in entries if e.get("triage") in order),
                  key=lambda e: (order[e["triage"]], str(e.get("name") or "")))


def apply_swipe(unified: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """Record a swipe on a unified record. Returns the same record."""
    if direction == SHORTLIST:
        unified["triage"] = SHORTLIST
    elif direction == REJECT:
        unified["triage"] = REJECT
        unified["review_status"] = "reject"
    elif direction == UNDO:
        unified.pop("triage", None)
        unified.pop("assigned_to", None)
        # Safe to restore: the deck only ever contains tbd applicants, so tbd
        # is where a left swipe found them.
        unified["review_status"] = "tbd"
    else:
        raise ValueError(f"Unknown swipe: {direction!r}")
    return unified


def assign(unified: Dict[str, Any], prof_id: str) -> Dict[str, Any]:
    """Put a shortlisted applicant in one professor's queue (exactly one — a
    second assignment moves them rather than adding)."""
    unified["assigned_to"] = prof_id
    return unified


def queues(entries: Sequence[Dict[str, Any]], professors: Sequence[Dict[str, str]]
           ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Each professor with the applicants assigned to them, plus the
    shortlisted-but-unassigned pile so nobody quietly falls through."""
    by_prof: Dict[str, List[Dict[str, Any]]] = {p["id"]: [] for p in professors}
    unassigned: List[Dict[str, Any]] = []
    for entry in shortlisted(entries):
        prof = entry.get("assigned_to") or ""
        if prof in by_prof:
            by_prof[prof].append(entry)
        else:
            unassigned.append(entry)
    out = [dict(p, applicants=by_prof[p["id"]]) for p in professors]
    return out, unassigned
