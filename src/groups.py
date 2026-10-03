"""Review groups: how an application travels after the grad office's pass.

    grad office  ──send to group──▶  group leader  ──assign──▶  reviewer
                                           ▲                       │
                                           └──── final call ◀── first call

1. The grad office (the `advisor` role) sends each shortlisted applicant to one
   **group** (`group` on the record).
2. The group's **leader** splits the group's applicants among the group's
   members (`assigned_to`), by hand or with "split evenly".
3. The **reviewer** makes the first call (`professor_status`, with an optional
   note for the leader).
4. The **leader** makes the final call (`final_status`), seeing the reviewer's
   call and note. A leader who reviews an applicant themselves makes one call
   that counts as both.

Groups live in config/groups.yaml and are made of people from the faculty list
(config/professors.yaml), referenced by their stable professor id, so renaming
a professor never breaks a group. A leader is simply a professor named as a
group's leader: no new kind of account, and leading two groups or reviewing in
one while leading another just works.

Each step keeps its own field, for the same reason the advisor's swipe and the
spreadsheet status are separate: when the reviewer and the leader disagree,
both calls stay visible. Moving an applicant upstream (to another group, or to
another reviewer) clears what was downstream of it, because a first call from
someone who no longer has the file isn't a call on record; a final call has to
be undone before the reviewer can change.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from .storage import pull_config, push_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUPS_YAML = (os.environ.get("GRADAPP_GROUPS_FILE", "").strip()
               or os.path.join(ROOT, "config", "groups.yaml"))

GROUP_FIELD = "group"
REVIEWER_FIELD = "assigned_to"           # name kept from the per-professor era
FIRST_FIELD = "professor_status"
FINAL_FIELD = "final_status"
ACCEPT, REJECT, UNDO = "accept", "reject", "undo"
CALLS = (ACCEPT, REJECT)

_FIRST_KEYS = (FIRST_FIELD, "professor_decided_at", "professor_note")
_FINAL_KEYS = (FINAL_FIELD, "final_decided_at", "final_by", "final_note")

# Where an applicant is in the group pipeline, in order.
NOT_SENT, TO_DISTRIBUTE, WITH_REVIEWER, READY_FOR_FINAL, DECIDED = (
    "not_sent", "to_distribute", "with_reviewer", "ready_for_final", "decided")
STAGE_LABELS = {
    NOT_SENT: "not sent to a group",
    TO_DISTRIBUTE: "waiting for the leader to assign a reviewer",
    WITH_REVIEWER: "waiting on the reviewer's first call",
    READY_FOR_FINAL: "waiting on the leader's final call",
    DECIDED: "final call made",
}

MAX_NOTE = 1000


class GroupError(ValueError):
    """Something the person needs to fix (unknown member, out-of-order call…)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_note(note: Any) -> str:
    return " ".join(str(note or "").split())[:MAX_NOTE]


# ------------------------------------------------------------------ config --

_YAML_HEADER = """\
# Review groups. The grad office sends each shortlisted applicant to one group;
# the group's leader assigns reviewers from its members and makes the final
# call. Editable here or from the app's "Groups" page; both write this file.
#
# `leader` and `members` are professor ids from professors.yaml. `id` is stored
# on applicants' records, so it is generated once and left alone: renaming a
# group is safe, changing its id orphans whoever was already sent to it.
#
# `areas` is optional and only used to suggest a group for an applicant.
"""


def load_groups(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Groups in file order. Missing file -> no groups, not a crash."""
    path = path or GROUPS_YAML
    pull_config(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        return []
    out = []
    for entry in data.get("groups") or []:
        if not entry.get("id") or not entry.get("name"):
            continue
        members = [str(m) for m in (entry.get("members") or []) if str(m).strip()]
        out.append({"id": str(entry["id"]), "name": str(entry["name"]),
                    "leader": str(entry.get("leader") or ""),
                    "members": list(dict.fromkeys(members)),
                    "areas": [str(a) for a in (entry.get("areas") or []) if str(a).strip()]})
    return out


def save_groups(groups: Sequence[Dict[str, Any]], path: Optional[str] = None) -> str:
    """Atomic write, like the faculty list: a half-written file would take the
    whole review step down, and people edit it by hand too."""
    path = path or GROUPS_YAML
    body = yaml.safe_dump(
        {"groups": [{"id": g["id"], "name": g["name"],
                     **({"leader": g["leader"]} if g.get("leader") else {}),
                     **({"members": list(g["members"])} if g.get("members") else {}),
                     **({"areas": list(g["areas"])} if g.get("areas") else {})}
                    for g in groups]},
        sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_YAML_HEADER + body)
    os.replace(tmp, path)
    push_config(path)
    return path


def group_by_id(gid: str, groups: Optional[Sequence[Dict[str, Any]]] = None):
    for g in (groups if groups is not None else load_groups()):
        if g["id"] == gid:
            return g
    return None


def reviewers(group: Dict[str, Any]) -> List[str]:
    """Who the leader can hand an applicant to: the members, and the leader."""
    ids = list(group.get("members") or [])
    if group.get("leader") and group["leader"] not in ids:
        ids.append(group["leader"])
    return ids


def led_by(prof_id: str, groups: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [g for g in groups if prof_id and g.get("leader") == prof_id]


def memberships(prof_id: str, groups: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [g for g in groups if prof_id and prof_id in reviewers(g)]


# ------------------------------------------------------ editing the groups --

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-") or "group"


def _clean_areas(areas: Any) -> List[str]:
    if isinstance(areas, str):
        areas = areas.split(",")
    return [a.strip() for a in (areas or []) if str(a).strip()]


def _vet_people(leader: str, members: Iterable[str],
                professors: Sequence[Dict[str, Any]]) -> Tuple[str, List[str]]:
    known = {p["id"] for p in professors}
    leader = str(leader or "").strip()
    members = list(dict.fromkeys(str(m).strip() for m in (members or []) if str(m).strip()))
    if leader and leader not in known:
        raise GroupError("The leader has to be someone on the faculty list.")
    unknown = [m for m in members if m not in known]
    if unknown:
        raise GroupError(f"Not on the faculty list: {', '.join(unknown)}")
    return leader, members


def add_group(groups: List[Dict[str, Any]], name: str, *, leader: str = "",
              members: Iterable[str] = (), areas: Any = None,
              professors: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    name = str(name or "").strip()
    if not name:
        raise GroupError("A group needs a name.")
    if any(g["name"].lower() == name.lower() for g in groups):
        raise GroupError(f"There's already a group called {name}.")
    leader, members = _vet_people(leader, members, professors)
    taken = {g["id"] for g in groups}
    gid, n = _slug(name), 2
    base = gid
    while gid in taken:
        gid, n = f"{base}-{n}", n + 1
    return list(groups) + [{"id": gid, "name": name, "leader": leader,
                            "members": members, "areas": _clean_areas(areas)}]


def update_group(groups: List[Dict[str, Any]], gid: str, *, name: Optional[str] = None,
                 leader: Optional[str] = None, members: Optional[Iterable[str]] = None,
                 areas: Any = None,
                 professors: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Change a group's details. The id never changes: records point at it."""
    out, found = [], False
    for g in groups:
        if g["id"] != gid:
            out.append(g)
            continue
        found = True
        new_name = g["name"] if name is None else str(name).strip()
        if not new_name:
            raise GroupError("A group needs a name.")
        if any(o["name"].lower() == new_name.lower() and o["id"] != gid for o in groups):
            raise GroupError(f"There's already a group called {new_name}.")
        new_leader, new_members = _vet_people(
            g["leader"] if leader is None else leader,
            g["members"] if members is None else members, professors)
        out.append({"id": gid, "name": new_name, "leader": new_leader,
                    "members": new_members,
                    "areas": g["areas"] if areas is None else _clean_areas(areas)})
    if not found:
        raise GroupError("No such group.")
    return out


def remove_group(groups: List[Dict[str, Any]], gid: str) -> List[Dict[str, Any]]:
    out = [g for g in groups if g["id"] != gid]
    if len(out) == len(groups):
        raise GroupError("No such group.")
    return out


def drop_professor(groups: List[Dict[str, Any]], prof_id: str) -> List[Dict[str, Any]]:
    """Take someone who left the faculty list out of every group."""
    return [dict(g, leader="" if g.get("leader") == prof_id else g.get("leader", ""),
                 members=[m for m in g.get("members") or [] if m != prof_id])
            for g in groups]


# ------------------------------------------------- what happens to a record --

def _pop(unified: Dict[str, Any], keys: Iterable[str]) -> None:
    for k in keys:
        unified.pop(k, None)


def send_to_group(unified: Dict[str, Any], gid: str) -> Dict[str, Any]:
    """The grad office's hand-off. Moving to a different group starts that
    group's pipeline over; an applicant who was assigned to a professor before
    groups existed keeps that reviewer when first sent to a group."""
    old = unified.get(GROUP_FIELD) or ""
    if gid == old:
        return unified
    if old or not gid:
        _pop(unified, (REVIEWER_FIELD,) + _FIRST_KEYS + _FINAL_KEYS)
    if gid:
        unified[GROUP_FIELD] = gid
    else:
        unified.pop(GROUP_FIELD, None)
    return unified


def assign_reviewer(unified: Dict[str, Any], prof_id: str,
                    group: Dict[str, Any]) -> Dict[str, Any]:
    """The leader hands the applicant to one member. Changing the reviewer
    drops the previous reviewer's first call; after the final call the
    reviewer is locked until the leader undoes it."""
    if unified.get(GROUP_FIELD) != group["id"]:
        raise GroupError("That applicant isn't in this group.")
    if unified.get(FINAL_FIELD):
        raise GroupError("The final call is already made; undo it before changing the reviewer.")
    if prof_id and prof_id not in reviewers(group):
        raise GroupError("That person isn't in this group.")
    if prof_id != (unified.get(REVIEWER_FIELD) or ""):
        _pop(unified, _FIRST_KEYS)
    if prof_id:
        unified[REVIEWER_FIELD] = prof_id
    else:
        unified.pop(REVIEWER_FIELD, None)
    return unified


def apply_first_call(unified: Dict[str, Any], decision: str, note: str = "") -> Dict[str, Any]:
    """The reviewer's accept/reject (with an optional note for the leader).
    Locked once the leader has made the final call."""
    if unified.get(FINAL_FIELD):
        raise GroupError("The group leader has already made the final call.")
    if decision == UNDO:
        _pop(unified, _FIRST_KEYS)
    elif decision in CALLS:
        unified[FIRST_FIELD] = decision
        unified["professor_decided_at"] = _now()
        note = clean_note(note)
        if note:
            unified["professor_note"] = note
        else:
            unified.pop("professor_note", None)
    else:
        raise ValueError(f"Unknown decision: {decision!r}")
    return unified


def apply_final_call(unified: Dict[str, Any], decision: str, *, leader_id: str,
                     by: str, note: str = "", group: Dict[str, Any]) -> Dict[str, Any]:
    """The leader's final call. Needs the reviewer's first call first, unless
    the leader is the reviewer, in which case this one call counts as both."""
    if unified.get(GROUP_FIELD) != group["id"]:
        raise GroupError("That applicant isn't in this group.")
    if decision == UNDO:
        _pop(unified, _FINAL_KEYS)
        return unified
    if decision not in CALLS:
        raise ValueError(f"Unknown decision: {decision!r}")
    if not unified.get(FIRST_FIELD):
        reviewer = unified.get(REVIEWER_FIELD) or ""
        if reviewer and reviewer == leader_id:
            unified[FIRST_FIELD] = decision
            unified["professor_decided_at"] = _now()
        elif not reviewer:
            raise GroupError("Assign a reviewer first; they make the first call.")
        else:
            raise GroupError("Still waiting on the reviewer's first call.")
    unified[FINAL_FIELD] = decision
    unified["final_decided_at"] = _now()
    unified["final_by"] = by
    note = clean_note(note)
    if note:
        unified["final_note"] = note
    else:
        unified.pop("final_note", None)
    return unified


# ------------------------------------------------------------- reading it --

def stage(entry: Dict[str, Any]) -> str:
    if not entry.get(GROUP_FIELD):
        return NOT_SENT
    if entry.get(FINAL_FIELD):
        return DECIDED
    if entry.get(FIRST_FIELD):
        return READY_FOR_FINAL
    if entry.get(REVIEWER_FIELD):
        return WITH_REVIEWER
    return TO_DISTRIBUTE


def in_group(entries: Iterable[Dict[str, Any]], gid: str) -> List[Dict[str, Any]]:
    return [e for e in entries if e.get(GROUP_FIELD) == gid]


def buckets(entries: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in STAGE_LABELS}
    for e in entries:
        out[stage(e)].append(e)
    return out


def board(entries: Sequence[Dict[str, Any]], groups: Sequence[Dict[str, Any]]
          ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Each group with its applicants bucketed by stage, plus applicants sent
    to a group id that no longer exists (so nobody silently disappears)."""
    known = {g["id"] for g in groups}
    out = []
    for g in groups:
        mine = in_group(entries, g["id"])
        out.append(dict(g, applicants=mine, stages=buckets(mine),
                        accepted=[e for e in mine if e.get(FINAL_FIELD) == ACCEPT],
                        rejected=[e for e in mine if e.get(FINAL_FIELD) == REJECT]))
    orphans = [e for e in entries if e.get(GROUP_FIELD) and e[GROUP_FIELD] not in known]
    return out, orphans


def _overlap(a: Iterable[str], b: Iterable[str]) -> int:
    norm = lambda xs: {str(x).strip().lower() for x in xs if str(x).strip()}
    return len(norm(a) & norm(b))


def suggest_group(entry: Dict[str, Any], groups: Sequence[Dict[str, Any]]) -> str:
    """The group whose areas best overlap the applicant's detected areas, or
    "" when nothing overlaps. A hint for the grad office, never applied."""
    areas = (entry.get("metrics") or {}).get("areas") or []
    best, score = "", 0
    for g in groups:
        s = _overlap(areas, g.get("areas") or [])
        if s > score:
            best, score = g["id"], s
    return best


def split_evenly(entries: Sequence[Dict[str, Any]], group: Dict[str, Any],
                 professors: Sequence[Dict[str, Any]] = ()) -> List[Tuple[str, str]]:
    """Hand every unassigned applicant in the group to a reviewer, keeping
    open workloads level. Members only (the leader isn't given a share unless
    they're listed as a member, or nobody else is). Ties go to the member whose
    areas match the applicant's best, then to list order.

    Returns [(cas_id, prof_id)] for the caller to write.
    """
    pool = list(group.get("members") or []) or ([group["leader"]] if group.get("leader") else [])
    if not pool:
        return []
    mine = in_group(entries, group["id"])
    load = {p: 0 for p in pool}
    for e in mine:
        who = e.get(REVIEWER_FIELD)
        if who in load and not e.get(FINAL_FIELD):
            load[who] += 1
    areas_of = {p["id"]: p.get("areas") or [] for p in professors}
    plan = []
    todo = sorted((e for e in mine if stage(e) == TO_DISTRIBUTE),
                  key=lambda e: str(e.get("name") or "").lower())
    for e in todo:
        applicant_areas = (e.get("metrics") or {}).get("areas") or []
        pick = min(pool, key=lambda p: (load[p], -_overlap(applicant_areas, areas_of.get(p, [])),
                                        pool.index(p)))
        load[pick] += 1
        plan.append((e["cas_id"], pick))
    return plan
