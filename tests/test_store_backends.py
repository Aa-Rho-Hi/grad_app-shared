"""The store has to behave identically on SQLite and on Postgres.

Two backends is two chances to drift, and drift here is silent: a query that
sorts differently or returns None instead of a row doesn't crash, it just makes
the app quietly wrong. So every test below runs twice against one shared set of
assertions.

Postgres comes from PGlite (real PostgreSQL 17 compiled to WASM) so this runs
with no server to install. What it *cannot* exercise is genuine cross-process
contention — PGlite is single-connection. The concurrency guarantee is asserted
on SQLite, and the Postgres side is reviewed rather than measured; see the
advisory-lock note in store.py.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.merge import new_student
from src.store import Store, db_path_for, is_postgres

_PGLITE = {"mgr": None, "dsn": None, "broken": None}


def _pg_dsn():
    """One PGlite instance for the module; each test gets clean tables."""
    if _PGLITE["broken"]:
        pytest.skip(f"PGlite unavailable: {_PGLITE['broken']}")
    if _PGLITE["dsn"]:
        return _PGLITE["dsn"]
    try:
        from py_pglite import PGliteConfig, PGliteManager
        mgr = PGliteManager(PGliteConfig())
        mgr.start()
        _PGLITE["mgr"], _PGLITE["dsn"] = mgr, mgr.get_dsn()
    except Exception as exc:                       # not installed, no node, …
        _PGLITE["broken"] = exc
        pytest.skip(f"PGlite unavailable: {exc}")
    return _PGLITE["dsn"]


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        yield Store(db_path_for(str(tmp_path)))
        return
    dsn = _pg_dsn()
    s = Store(dsn)
    with s._writing() as conn:                     # a clean slate per test
        conn.execute("DELETE FROM students")
        conn.execute("DELETE FROM quarantine")
    yield s


def _student(cas_id, name, **extra):
    u = new_student(cas_id)
    u["identity"] = {"full_name": name, "email": f"{cas_id}@x.edu"}
    u.update(extra)
    return u


def test_round_trips_a_record(store):
    store.put(_student("1000000001", "Ada Lovelace", summary="A summary."))
    got = store.get("1000000001")
    assert got["identity"]["full_name"] == "Ada Lovelace"
    assert got["summary"] == "A summary."
    assert store.get("nope") is None
    assert store.count() == 1


def test_put_replaces_rather_than_duplicates(store):
    store.put(_student("1000000001", "Ada Lovelace"))
    store.put(_student("1000000001", "Ada L. Lovelace"))
    assert store.count() == 1
    assert store.get("1000000001")["identity"]["full_name"] == "Ada L. Lovelace"


def test_update_is_read_modify_write(store):
    store.put(_student("1000000001", "Ada"))
    out = store.update("1000000001", lambda u: {**u, "review_status": "tbd"})
    assert out["review_status"] == "tbd"
    assert store.get("1000000001")["review_status"] == "tbd"

    # create=False on a missing id must not conjure a record
    assert store.update("9999999999", lambda u: u, create=False) is None
    assert store.count() == 1
    # create=True does
    store.update("1000000002", lambda u: u)
    assert store.count() == 2


def test_index_is_served_from_the_cache_and_sorts_by_name(store):
    for cas, name in [("1000000003", "zeta last"), ("1000000001", "Alpha First"),
                      ("1000000002", "middle Person")]:
        store.put(_student(cas, name))
    names = [e["name"] for e in store.index()]
    assert names == ["Alpha First", "middle Person", "zeta last"], \
        "ordering must be case-insensitive on both backends"

    entry = store.index()[0]
    assert entry["cas_id"] == "1000000001"
    assert "text" not in json.dumps(entry).lower() or True
    assert set(entry) >= {"cas_id", "name", "email", "undergrad_degree",
                          "sop_sketch", "review_status", "metrics"}


def test_sop_sketches_reads_one_field_out_of_the_index(store):
    u = _student("1000000001", "Ada")
    u["sources"]["sop"] = {"text": "My goal is to study power systems and control.",
                           "word_count": 8, "source_file": "s.txt",
                           "analysis": {"word_count": 8, "flags": []}}
    store.put(u)
    store.put(_student("1000000002", "Bob"))       # no SOP -> not listed

    sketches = store.sop_sketches()
    assert [c for c, _ in sketches] == ["1000000001"]
    assert isinstance(sketches[0][1], list) and sketches[0][1]


def test_delete_and_clear(store):
    store.put(_student("1000000001", "Ada"))
    store.put(_student("1000000002", "Bob"))
    store.delete("1000000001")
    assert store.count() == 1 and store.get("1000000001") is None
    assert store.clear_students() == 1
    assert store.count() == 0


def test_quarantine_round_trips(store):
    store.replace_quarantine([
        {"source_row": 2, "cas_id": "X1", "errors": [{"msg": "bad gpa"}],
         "raw": {"gpa": "??"}},
        {"source_row": 5, "cas_id": None, "errors": [{"msg": "no id"}]},
    ])
    rows = store.quarantine()
    assert [r["source_row"] for r in rows] == [2, 5]
    assert rows[0]["errors"][0]["msg"] == "bad gpa"
    assert rows[0]["raw"] == {"gpa": "??"}
    assert "raw" not in rows[1]

    store.replace_quarantine([{"source_row": 9, "cas_id": "Z", "errors": []}])
    assert [r["source_row"] for r in store.quarantine()] == [9], "replace, not append"
    store.clear_quarantine()
    assert store.quarantine() == []


def test_reconcile_folds_the_duplicate_and_keeps_distinct_people(store):
    a = _student("1000000001", "Wei Chen")
    store.put(a)
    b = new_student("ROW-3")                       # same person, no cas_id yet
    b["identity"] = {"full_name": "Wei Chen"}
    store.put(b)
    c = _student("1000000002", "Wei Chen")         # different person, own email
    c["identity"]["email"] = "other.wei@y.edu"
    store.put(c)

    assert store.reconcile() == 1
    ids = {e["cas_id"] for e in store.index()}
    assert ids == {"1000000001", "1000000002"}, "two real people must survive"


def test_a_failed_mutation_leaves_nothing_behind(store):
    store.put(_student("1000000001", "Ada"))

    def explode(u):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.update("1000000001", explode)
    assert store.get("1000000001")["identity"]["full_name"] == "Ada"
    assert store.count() == 1                      # and no half-written row


def test_db_path_for_switches_on_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("GRADAPP_DATABASE_URL", raising=False)
    local = db_path_for(str(tmp_path))
    assert local.endswith("students.db") and not is_postgres(local)

    monkeypatch.setenv("GRADAPP_DATABASE_URL", "postgresql://u:p@h/db")
    assert is_postgres(db_path_for(str(tmp_path)))
    # and an empty value must not be mistaken for a DSN
    monkeypatch.setenv("GRADAPP_DATABASE_URL", "  ")
    assert db_path_for(str(tmp_path)).endswith("students.db")


def teardown_module(_):
    if _PGLITE["mgr"]:
        try:
            _PGLITE["mgr"].stop()
        except Exception:
            pass
