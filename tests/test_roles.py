"""Access by role, route by route.

Three roles (src/users.py):

    Grad office   everything, except a group's calls
    Group leader  only the groups they're named a leader of (and their own reviews)
    Professor     only the applicants assigned to them

Every route is hit as every role here, so a page that leaks to the wrong role
fails the suite instead of waiting for someone to notice. A new route that
isn't added to one of these lists is caught by the last test.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import groups as G
from src import users as U

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "tests", "fixtures", "sample_applications.csv")
FIELD_MAP = os.path.join(ROOT, "config", "csv_field_map.yaml")

# Grad office only. GETs and POSTs; nobody else gets past the door.
OFFICE_GET = ["/step/1", "/step/2", "/step/3", "/applicants", "/triage", "/triage/cards",
              "/triage/shortlist", "/summaries", "/prescreen", "/professors",
              "/professors?edit=1", "/groups", "/groups?edit=1", "/accounts",
              "/download/status.xlsx", "/download/summaries", "/download/prescreen.zip",
              "/download/prescreen/summary.xlsx", "/review-sheet"]
OFFICE_POST = ["/upload", "/process", "/upload_zip", "/upload_document", "/reset",
               "/step/3", "/triage/swipe", "/triage/assign", "/prescreen/reapply",
               "/professors/add", "/professors/update", "/professors/delete",
               "/groups/add", "/groups/update", "/groups/delete",
               "/accounts/approve", "/accounts/role", "/accounts/remove"]
# Everything else that isn't open to the public, checked one by one below.
FACULTY_ROUTES = {"/", "/prof", "/prof/decide", "/group/<gid>", "/group/<gid>/assign",
                  "/group/<gid>/split", "/group/<gid>/final", "/student/<cas_id>"}
# Upload progress: the grad office's uploads, so the grad office's to poll.
OFFICE_PARAM = {"/jobs/<job_id>", "/download/prescreen/<kind>.xlsx"}
OPEN_ROUTES = {"/login", "/logout", "/signup", "/setup", "/tasks/run", "/static/<path:filename>"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Roster on file; group Systems led by Paul Gratz with member Zhiwen Fan;
    applicant A sent there and assigned to Zhiwen, applicant B not sent
    anywhere. Logins: grad office, the leader, the member, an outsider."""
    from src import app as appmod
    from src.run import run
    from src.store import Store, db_path_for
    monkeypatch.setattr(appmod, "DATA", str(tmp_path))
    os.makedirs(tmp_path / "raw", exist_ok=True)
    appmod.app.config.update(TESTING=True)
    run(CSV, str(tmp_path), FIELD_MAP)
    G.save_groups([{"id": "systems", "name": "Systems", "leaders": ["gratz-paul"],
                    "members": ["fan-zhiwen"], "areas": []}])
    store = Store(db_path_for(str(tmp_path)))
    a, b = [e["cas_id"] for e in store.index()][:2]
    store.update(a, lambda u: {**u, "triage": "shortlist", "group": "systems",
                               "assigned_to": "fan-zhiwen"})
    store.update(b, lambda u: {**u, "triage": "shortlist"})
    people = U.add_user([], "office", "pw", U.ADVISOR)
    people = U.add_user(people, "lead", "pw", U.LEADER, "gratz-paul")
    people = U.add_user(people, "prof", "pw", U.PROFESSOR, "fan-zhiwen")
    people = U.add_user(people, "outsider", "pw", U.PROFESSOR, "duffield-nick")
    U.save_users(people, U.USERS_YAML)
    return appmod, store, a, b


def _login(appmod, who):
    client = appmod.app.test_client()
    assert client.post("/login", data={"username": who, "password": "pw"}).status_code == 302
    return client


@pytest.mark.parametrize("who", ["lead", "prof", "outsider"])
def test_grad_office_pages_are_closed_to_faculty(world, who):
    appmod, _store, _a, _b = world
    client = _login(appmod, who)
    for url in OFFICE_GET:
        assert client.get(url).status_code == 403, (who, url)
    for url in OFFICE_POST:
        assert client.post(url, data={}).status_code == 403, (who, url)
    assert client.get("/jobs/whatever").status_code == 403


def test_the_grad_office_reaches_its_pages(world):
    appmod, _store, _a, _b = world
    client = _login(appmod, "office")
    for url in OFFICE_GET:
        assert client.get(url).status_code in (200, 302), url


def test_faculty_routes_by_role(world):
    appmod, store, a, b = world
    fetch = {"X-Requested-With": "fetch"}
    office, lead = _login(appmod, "office"), _login(appmod, "lead")
    prof, outsider = _login(appmod, "prof"), _login(appmod, "outsider")

    # where each lands
    assert office.get("/").status_code == 200
    for c in (lead, prof, outsider):
        assert c.get("/").headers["Location"].endswith("/prof")
    assert office.get("/prof").status_code == 302          # nobody's queue
    for c in (lead, prof, outsider):
        assert c.get("/prof").status_code == 200

    # reading applicant files
    assert office.get(f"/student/{a}").status_code == 200
    assert lead.get(f"/student/{a}").status_code == 200     # their group's
    assert prof.get(f"/student/{a}").status_code == 200     # assigned to them
    assert outsider.get(f"/student/{a}").status_code == 403
    assert office.get(f"/student/{b}").status_code == 200
    for c in (lead, prof, outsider):
        assert c.get(f"/student/{b}").status_code == 403    # in nobody's group

    # the group workspace: the office looks, only the leader acts
    assert office.get("/group/systems").status_code == 200
    assert lead.get("/group/systems").status_code == 200
    for c in (prof, outsider):
        assert c.get("/group/systems").status_code == 403
    for url, data in [("/group/systems/assign", {"cas_id": a, "reviewer": "fan-zhiwen"}),
                      ("/group/systems/split", {}),
                      ("/group/systems/final", {"cas_id": a, "decision": "accept"})]:
        for c in (office, prof, outsider):
            assert c.post(url, data=data, headers=fetch).status_code == 403, url
    # the leader gets in (409 here only because nobody has made the first call)
    assert lead.post("/group/systems/final", data={"cas_id": a, "decision": "accept"},
                     headers=fetch).status_code == 409

    # first calls: only the assigned reviewer
    for c in (office, lead, outsider):
        assert c.post("/prof/decide", data={"cas_id": a, "decision": "accept"}).status_code == 403
    assert prof.post("/prof/decide", data={"cas_id": a, "decision": "accept"}).status_code == 200
    assert lead.post("/group/systems/final", data={"cas_id": a, "decision": "accept"},
                     headers=fetch).status_code == 200


def test_the_grad_office_cannot_act_as_a_professor(world):
    """?as= picks a queue in no-login mode; with logins it must not let the
    grad office make a professor's first call."""
    appmod, store, a, _b = world
    office = _login(appmod, "office")
    assert office.get("/prof?as=fan-zhiwen").status_code == 302
    assert office.post("/prof/decide?as=fan-zhiwen",
                       data={"cas_id": a, "decision": "accept"}).status_code == 403
    assert "professor_status" not in store.get(a)


def test_an_empty_reviewer_never_matches_an_unassigned_applicant(tmp_path, monkeypatch):
    """No logins, no ?as=: there is no reviewer, so no first call — the empty
    id used to equal the empty `assigned_to` of everyone not yet assigned."""
    from src import app as appmod
    from src.run import run
    from src.store import Store, db_path_for
    monkeypatch.setattr(appmod, "DATA", str(tmp_path))
    appmod.app.config.update(TESTING=True)
    run(CSV, str(tmp_path), FIELD_MAP)
    store = Store(db_path_for(str(tmp_path)))
    cas = store.index()[0]["cas_id"]
    r = appmod.app.test_client().post("/prof/decide", data={"cas_id": cas, "decision": "accept"})
    assert r.status_code == 403 and "professor_status" not in store.get(cas)


def test_leading_takes_the_role_and_the_group(world):
    """A Group leader login leads only groups it's named on; a professor login
    named on a group leads nothing until the role changes."""
    appmod, store, a, _b = world
    people = U.add_user(U.load_users(), "lead2", "pw", U.LEADER, "narayanan-krishna")
    U.save_users(people)
    other_leader = _login(appmod, "lead2")
    assert other_leader.get("/group/systems").status_code == 403
    assert other_leader.get(f"/student/{a}").status_code == 403

    # name the professor-role outsider as a co-leader: still a professor login
    G.save_groups([dict(G.load_groups()[0], leaders=["gratz-paul", "duffield-nick"])])
    outsider = _login(appmod, "outsider")
    assert outsider.get("/group/systems").status_code == 403
    assert "your login is a Professor login" in outsider.get("/prof").get_data(as_text=True)

    # the grad office changes the role; it applies on the very next request
    office = _login(appmod, "office")
    office.post("/accounts/role", data={"username": "outsider", "role": "leader"})
    assert outsider.get("/group/systems").status_code == 200


def test_any_co_leader_can_make_the_final_call(world):
    appmod, store, a, _b = world
    G.save_groups([dict(G.load_groups()[0], leaders=["gratz-paul", "narayanan-krishna"])])
    people = U.add_user(U.load_users(), "lead2", "pw", U.LEADER, "narayanan-krishna")
    U.save_users(people)
    _login(appmod, "prof").post("/prof/decide", data={"cas_id": a, "decision": "accept"})
    _login(appmod, "lead2").post("/group/systems/final",
                                 data={"cas_id": a, "decision": "reject"})
    rec = store.get(a)
    assert rec["final_status"] == "reject" and rec["final_by"] == "lead2"


def test_role_changes_and_removals_apply_to_open_sessions(world):
    appmod, store, a, _b = world
    lead, prof, office = _login(appmod, "lead"), _login(appmod, "prof"), _login(appmod, "office")
    assert lead.get("/group/systems").status_code == 200
    office.post("/accounts/role", data={"username": "lead", "role": "professor"})
    assert lead.get("/group/systems").status_code == 403      # no sign-out needed

    # a removed account's live session is nobody, not everybody
    office.post("/accounts/remove", data={"username": "prof"})
    for url in ("/prof", "/applicants", "/accounts", f"/student/{a}"):
        r = prof.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"], url


def test_there_is_always_a_grad_office_login():
    people = U.add_user([], "office", "pw", U.ADVISOR)
    people = U.add_user(people, "prof", "pw", U.PROFESSOR, "fan-zhiwen")
    with pytest.raises(U.UserError, match="Grad office"):
        U.set_role(people, "office", U.PROFESSOR, "gratz-paul")
    with pytest.raises(U.UserError, match="Grad office"):
        U.remove_user(people, "office")
    people = U.add_user(people, "office2", "pw", U.ADVISOR)
    assert U.set_role(people, "office", U.LEADER, "gratz-paul")      # another remains


def test_role_rules():
    people = U.add_user([], "office", "pw", U.ADVISOR)
    with pytest.raises(U.UserError):
        U.add_user(people, "x", "pw", U.LEADER)                     # leaders are faculty
    people = U.add_user(people, "lead", "pw", U.LEADER, "gratz-paul")
    with pytest.raises(U.UserError, match="already signs in"):
        U.add_user(people, "y", "pw", U.PROFESSOR, "gratz-paul")    # one login each
    demoted = U.set_role(people, "lead", U.PROFESSOR)                # keeps the link
    assert next(u for u in demoted if u["username"] == "lead")["professor"] == "gratz-paul"
    office = U.set_role(people, "lead", U.ADVISOR)
    assert next(u for u in office if u["username"] == "lead")["professor"] == ""


def test_every_route_is_accounted_for():
    """A new route has to be put in one of the lists above (and so get a role
    check) before the suite passes."""
    from src import app as appmod
    rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    known = set(OFFICE_GET) | set(OFFICE_POST) | FACULTY_ROUTES | OPEN_ROUTES | OFFICE_PARAM
    known = {k.split("?")[0] for k in known}
    missing = sorted(r for r in rules if r not in known)
    assert not missing, f"routes with no role decision in tests/test_roles.py: {missing}"
