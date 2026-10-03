"""Accounts and roles — one app, three jobs.

    Grad office   (`advisor`)  everything: setup (accounts, faculty, groups,
                               rules), uploads, prescreen, triage, sending
                               applicants to groups, every report. Can look at
                               a group's page but never makes a group's calls.
    Group leader  (`leader`)   the groups they're named a leader of, and only
                               those: hand applicants to members, make the
                               final call. Also reviews like a professor.
    Professor     (`professor`) only the applicants assigned to them: the
                               first call.

Leading a group takes both the Group leader role here and being named a
leader of that group in config/groups.yaml. Either alone isn't enough: a role
with no group leads nothing, and a professor login named on a group can't act
for it until the grad office changes the role. The stored value for the grad
office is still `advisor` so existing users.yaml files keep working.

Login is **off until you create a user**. A single reviewer on a laptop
shouldn't have to authenticate to themselves, and every existing route keeps
working with no accounts configured; the moment `config/users.yaml` has someone
in it, every page requires a session. That switch is deliberate — a half-set-up
login that silently lets everyone in is worse than none.

Passwords are stored as salted PBKDF2 hashes (werkzeug), never in the clear.
Manage them from the CLI:

    python -m src.users add --username maya --role advisor --password ...
    python -m src.users add --username pgratz --role professor \\
                            --professor gratz-paul --password ...
    python -m src.users demo --password letmein     # one login per professor
    python -m src.users list
    python -m src.users approve --username pgratz --role professor \\
                                --professor gratz-paul

People can also request an account themselves from the sign-in page. A request
carries a real password but **no role**, so it can't open anything until an
advisor approves it and says what it is.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from .storage import pull_config, push_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Overridable because in a container config/ is part of the image and therefore
# ephemeral: an account created at /setup would be gone on the next deploy.
# Point this inside the data directory and storage.py keeps it in the bucket.
USERS_YAML = (os.environ.get("GRADAPP_USERS_FILE", "").strip()
              or os.path.join(ROOT, "config", "users.yaml"))

ADVISOR, LEADER, PROFESSOR = "advisor", "leader", "professor"
ROLES = (ADVISOR, LEADER, PROFESSOR)
#: Roles whose login stands for one person on the faculty list.
FACULTY_ROLES = (LEADER, PROFESSOR)

ROLE_LABELS = {ADVISOR: "Grad office", LEADER: "Group leader", PROFESSOR: "Professor",
               "pending": "Waiting for approval"}

#: What each role can do, in words. Shown on the Accounts page; the routes in
#: app.py enforce it, and tests/test_roles.py checks every route against it.
ROLE_ACCESS = [
    (ADVISOR, "Everything: accounts, faculty list, groups and rules; uploads, prescreen, "
              "triage, sending applicants to groups; every applicant and every report. "
              "Can open a group's page but can't make its calls."),
    (LEADER, "Only the groups they're named a leader of: see those applicants, hand them "
             "to members, make the final call. Also reviews applicants assigned to them."),
    (PROFESSOR, "Only applicants assigned to them: read the file, make the first call "
                "with a note for the leader."),
]

#: A self-signup that an advisor hasn't approved yet. Deliberately stored *as
#: the role* rather than as an `approved: false` flag beside a real one: every
#: gate in the app asks "is your role advisor/professor?", so a pending account
#: fails all of them by construction. A boolean flag would mean an account that
#: already holds a real role and is only kept out by a check someone could
#: forget to write — that fails open, this fails closed.
PENDING = "pending"
STORED_ROLES = ROLES + (PENDING,)

_HEADER = """\
# Accounts. Delete every entry here and the app goes back to no-login mode.
#
#   role: advisor    -> the grad office: everything (setup, uploads, triage,
#                       sending applicants to groups, reports)
#   role: leader     -> group leader: their groups' applicants, hand-out and
#                       the final call; needs `professor` like a professor does
#   role: professor  -> only the students assigned to them; needs `professor`
#                       to name which entry in professors.yaml they are
#
# Passwords are PBKDF2 hashes. Manage with `python -m src.users`.
"""


class UserError(ValueError):
    """Something the operator needs to fix (bad role, unknown professor…)."""


def load_users(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or USERS_YAML
    pull_config(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        return []
    out = []
    for entry in data.get("users") or []:
        if not entry.get("username") or entry.get("role") not in STORED_ROLES:
            continue
        out.append({"username": str(entry["username"]),
                    "role": str(entry["role"]),
                    "professor": str(entry.get("professor") or ""),
                    "password_hash": str(entry.get("password_hash") or "")})
    return out


def save_users(users, path: Optional[str] = None) -> str:
    path = path or USERS_YAML
    body = yaml.safe_dump(
        {"users": [{"username": u["username"], "role": u["role"],
                    **({"professor": u["professor"]} if u.get("professor") else {}),
                    "password_hash": u["password_hash"]} for u in users]},
        sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_HEADER + body)
    os.replace(tmp, path)
    push_config(path)
    return path


def approved_users(users: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [u for u in users if u["role"] in ROLES]


def pending_users(users: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [u for u in users if u["role"] == PENDING]


def login_required_globally(path: Optional[str] = None) -> bool:
    """True once a *usable* account exists. No accounts = single-user mode.

    Pending signups don't count, and that's load-bearing. If they did, a
    stranger hitting /signup on an app running in no-login mode would flip it
    to login-required — locking out the advisor, who by definition has no
    account yet. Nobody could get in and it would need the CLI to recover.
    """
    return bool(approved_users(load_users(path)))


def authenticate(username: str, password: str,
                 path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The matching user, or None. Constant-ish work either way: an unknown
    username still runs a hash check so it can't be told apart by timing.

    A pending account authenticates successfully and comes back with
    ``role == PENDING``; it's the caller's job to refuse it. That's so the
    "waiting for approval" message can be shown only to someone who already
    proved they know the password — telling anyone who types a username that it
    exists would turn the signup form into a way to enumerate staff.
    """
    users = load_users(path)
    found = next((u for u in users if u["username"] == (username or "").strip()), None)
    reference = found["password_hash"] if found else generate_password_hash("x")
    ok = check_password_hash(reference, password or "")
    return found if (found and ok) else None


def linked_professors(users: List[Dict[str, Any]], *, ignoring: str = "") -> Dict[str, str]:
    """{professor id: the username already holding it}."""
    return {u["professor"]: u["username"] for u in users
            if u["professor"] and u["role"] in FACULTY_ROLES
            and u["username"] != ignoring}


def _vet_professor(role: str, professor: str,
                   users: Optional[List[Dict[str, Any]]] = None,
                   username: str = "") -> str:
    """A professor login must point at exactly one real person on the faculty list.

    Two rules, and both exist because the link is what tells the app whose
    queue to show:

    * The id has to exist. An account naming one that doesn't isn't a small
      inconsistency — the person signs in, lands on their deck, and sees an
      empty queue forever with nothing to say why.
    * The id can't already be taken. Pointing a second account at a professor
      silently hands someone else that professor's applicants, which is an
      access-control decision dressed up as a dropdown choice. If a professor
      genuinely needs a new login, remove the old account first — deliberate
      and visible, rather than two accounts nobody remembers creating.
    """
    if role not in FACULTY_ROLES:
        return ""
    from .triage import load_professors        # local: triage imports upward
    faculty = load_professors()
    if not faculty:
        raise UserError(
            "There are no professors on the faculty list yet. Add one under "
            "Faculty first — a professor or group leader login has to point at "
            "someone who exists.")
    if not professor:
        raise UserError("A professor or group leader account has to name "
                        "someone on the faculty list.")
    if not any(p["id"] == professor for p in faculty):
        raise UserError(f"No professor with id {professor!r} on the faculty "
                        "list (see config/professors.yaml).")
    taken = linked_professors(users or [], ignoring=username)
    if professor in taken:
        name = next((p["name"] for p in faculty if p["id"] == professor), professor)
        raise UserError(
            f"{name} already signs in as '{taken[professor]}'. Remove that "
            "account first if they need a new one — two logins on one queue "
            "means two people seeing the same applicants.")
    return professor


def _vet_new(users: List[Dict[str, Any]], username: str, password: str) -> str:
    username = (username or "").strip()
    if not username:
        raise UserError("A user needs a username.")
    if not password:
        raise UserError("A user needs a password.")
    if any(u["username"].lower() == username.lower() for u in users):
        raise UserError(f"{username} already has an account.")
    return username


def add_user(users: List[Dict[str, Any]], username: str, password: str, role: str,
             professor: str = "") -> List[Dict[str, Any]]:
    if role not in ROLES:
        raise UserError(f"Role must be one of {', '.join(ROLES)}.")
    professor = _vet_professor(role, professor, users)
    username = _vet_new(users, username, password)
    return list(users) + [{"username": username, "role": role,
                           "professor": professor,
                           "password_hash": generate_password_hash(password)}]


def request_account(users: List[Dict[str, Any]], username: str,
                    password: str) -> List[Dict[str, Any]]:
    """A self-signup: a real password hash, but no role until someone grants it.

    The requester doesn't get to say what they are. Role is assigned at
    approval by an advisor — otherwise 'create account' would just be 'choose
    your own access level'.
    """
    username = _vet_new(users, username, password)
    return list(users) + [{"username": username, "role": PENDING, "professor": "",
                           "password_hash": generate_password_hash(password)}]


def approve_user(users: List[Dict[str, Any]], username: str, role: str,
                 professor: str = "") -> List[Dict[str, Any]]:
    """Grant a pending request the role an advisor picked for it."""
    if role not in ROLES:
        raise UserError(f"Role must be one of {', '.join(ROLES)}.")
    professor = _vet_professor(role, professor, users, username)
    out, hit = [], False
    for u in users:
        if u["username"] == username and u["role"] == PENDING:
            out.append({**u, "role": role, "professor": professor})
            hit = True
        else:
            out.append(u)
    if not hit:
        raise UserError("No pending request for that username.")
    return out


def _keeps_a_grad_office(before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> None:
    """Refuse a change that leaves working logins but no grad office login:
    nobody could then approve, change or remove anyone, and getting back in
    would need the command line on the server."""
    had = any(u["role"] == ADVISOR for u in before)
    if had and approved_users(after) and not any(u["role"] == ADVISOR for u in after):
        raise UserError("At least one Grad office login has to remain.")


def set_role(users: List[Dict[str, Any]], username: str, role: str,
             professor: str = "") -> List[Dict[str, Any]]:
    """Change an existing account's role (e.g. make a professor a group
    leader). Faculty roles keep or take a professor link; the grad office
    role drops it."""
    if role not in ROLES:
        raise UserError(f"Role must be one of {', '.join(ROLES)}.")
    current = next((u for u in users if u["username"] == username), None)
    if not current or current["role"] == PENDING:
        raise UserError("No such account (approve a pending request instead).")
    if role in FACULTY_ROLES and not professor:
        professor = current.get("professor", "")
    professor = _vet_professor(role, professor, users, username)
    out = [{**u, "role": role, "professor": professor} if u["username"] == username else u
           for u in users]
    _keeps_a_grad_office(users, out)
    return out


def remove_user(users: List[Dict[str, Any]], username: str) -> List[Dict[str, Any]]:
    out = [u for u in users if u["username"] != username]
    if len(out) == len(users):
        raise UserError("No such user.")
    _keeps_a_grad_office(users, out)
    return out


def main() -> None:
    from .triage import load_professors

    ap = argparse.ArgumentParser(description="Manage grad-app accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="create an account")
    a.add_argument("--username", required=True)
    a.add_argument("--password", required=True)
    a.add_argument("--role", required=True, choices=ROLES)
    a.add_argument("--professor", default="",
                   help="professor id, for role=professor or role=leader")

    s_ = sub.add_parser("role", help="change an account's role")
    s_.add_argument("--username", required=True)
    s_.add_argument("--role", required=True, choices=ROLES)
    s_.add_argument("--professor", default="", help="professor id, for leader/professor")

    r = sub.add_parser("remove", help="delete an account (or reject a request)")
    r.add_argument("--username", required=True)

    ap_ = sub.add_parser("approve", help="grant a pending signup a role")
    ap_.add_argument("--username", required=True)
    ap_.add_argument("--role", required=True, choices=ROLES)
    ap_.add_argument("--professor", default="",
                     help="professor id, for role=professor or role=leader")

    d = sub.add_parser("demo", help="an advisor plus one login per professor")
    d.add_argument("--password", required=True, help="used for every demo account")

    sub.add_parser("list", help="show accounts (never passwords)")
    args = ap.parse_args()

    users = load_users()
    if args.cmd == "add":
        # The faculty-list check lives in add_user now, so the CLI and the web
        # can't disagree about what a valid professor account looks like.
        save_users(add_user(users, args.username, args.password, args.role, args.professor))
        print(f"Added {args.username} ({args.role}).")
    elif args.cmd == "role":
        save_users(set_role(users, args.username, args.role, args.professor))
        print(f"{args.username} is now {ROLE_LABELS[args.role]}.")
    elif args.cmd == "remove":
        save_users(remove_user(users, args.username))
        print(f"Removed {args.username}.")
    elif args.cmd == "approve":
        save_users(approve_user(users, args.username, args.role, args.professor))
        print(f"Approved {args.username} as {args.role}.")
    elif args.cmd == "demo":
        users = add_user([], "advisor", args.password, ADVISOR)
        for prof in load_professors():
            users = add_user(users, prof["id"], args.password, PROFESSOR, prof["id"])
        save_users(users)
        print("Created: advisor, " + ", ".join(p["id"] for p in load_professors()))
        print("Every account uses the password you passed. Change them before "
              "this leaves your machine.")
    else:
        if not approved_users(users):
            print("No usable accounts — the app is in no-login mode.")
        for u in approved_users(users):
            who = f" -> {u['professor']}" if u["professor"] else ""
            print(f"{u['username']:<20} {u['role']}{who}")
        waiting = pending_users(users)
        if waiting:
            print(f"\n{len(waiting)} waiting for approval "
                  "(`users approve --username … --role …`):")
            for u in waiting:
                print(f"  {u['username']}")


if __name__ == "__main__":
    main()
