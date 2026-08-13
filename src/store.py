"""SQLite-backed storage for unified student records — atomic & concurrency-safe.

Replaces the earlier "one JSON file per student" store. Every unified record is
a row keyed by ``cas_id`` with the record itself kept as JSON, so the data shape
is unchanged — only persistence moves into a transactional database.

Why a database:
  * **Atomic writes.** A record is written in a single transaction, so a crash
    mid-write can never leave a half-written record on disk (the old per-file
    JSON writer could).
  * **Concurrency.** ``update()`` opens a ``BEGIN IMMEDIATE`` transaction, so a
    read-modify-write on one student serializes against any other writer instead
    of two uploads clobbering each other ("lost update"). WAL mode lets readers
    (the web UI) keep working while a write is in flight.

Filesystem robustness: WAL needs a shared-memory file and SQLite needs POSIX
locks, which some networked/synced/FUSE mounts don't provide (writes raise
"disk I/O error"). At init the store probes the filesystem and degrades
gracefully: WAL -> rollback journal -> a no-OS-lock mode where a per-path
in-process lock provides the serialization instead. On an ordinary local disk it
just uses WAL.

Still fully local and deterministic: SQLite is an in-process file database, no
server and no network.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import quote

from .merge import (applicant_metrics, merge_identity, new_student,
                    sources_present, summary_view, _is_cas_id, _norm_name)
from .analyze import sketch
from .review import undergrad_degree
from .template import render

DB_FILENAME = "students.db"

# Per-database-path config + lock, resolved once per process.
_DB_CONFIG: Dict[str, Dict[str, Any]] = {}   # path -> {"journal": str, "nolock": bool}
_DB_LOCKS: Dict[str, threading.RLock] = {}
_CONFIG_GUARD = threading.Lock()


def is_postgres(target: str) -> bool:
    return target.startswith(("postgres://", "postgresql://"))


def db_path_for(outdir: str) -> str:
    """Where the records live.

    Returns a DSN when GRADAPP_DATABASE_URL is set (Cloud SQL) and a file path
    otherwise. Deliberately the same function every call site already uses, so
    switching backends changes one environment variable and no code — the
    alternative was threading a backend choice through a dozen constructors,
    which is a dozen chances to miss one and silently write to the wrong place.
    """
    dsn = os.environ.get("GRADAPP_DATABASE_URL", "").strip()
    return dsn or os.path.join(outdir, DB_FILENAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# db_path -> chosen journal mode, set once per process so we don't re-probe the
# filesystem on every Store() construction.
_INIT_DONE: Dict[str, str] = {}


_DDL = (
    "CREATE TABLE IF NOT EXISTS students ("
    " cas_id TEXT PRIMARY KEY,"
    " record TEXT NOT NULL,"
    " name   TEXT,"
    " index_json TEXT,"
    " updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS jobs ("
    " id TEXT PRIMARY KEY,"
    " state TEXT NOT NULL,"
    " updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS quarantine ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " source_row INTEGER,"
    " cas_id TEXT,"
    " errors TEXT,"
    " raw TEXT,"
    " created_at TEXT)",
)

# Columns added after the first release; applied with ALTER TABLE so existing
# databases upgrade in place (the error when the column already exists is
# ignored). index_json caches the lightweight per-student list-view entry so
# the UI never has to deserialize full records (with complete SOP/LOR text)
# just to render the applicant list.
_MIGRATIONS = (
    "ALTER TABLE students ADD COLUMN index_json TEXT",
)


def _apply_ddl(conn: sqlite3.Connection) -> None:
    for ddl in _DDL:
        conn.execute(ddl)
    for mig in _MIGRATIONS:
        try:
            conn.execute(mig)
        except sqlite3.OperationalError:
            pass   # column already exists


# --------------------------------------------------------------- Postgres --
#
# Cloud SQL. The record shape, the index_json cache and every query below are
# the same; what differs is the dialect and how "one writer at a time" is
# obtained. SQLite gets it from BEGIN IMMEDIATE taking the database's write
# lock. Postgres has no equivalent, and row locks aren't enough here: update()
# does read-modify-write and may be *creating* the row, so there is nothing to
# lock yet and two concurrent creators would both insert with the last one
# winning. A transaction-scoped advisory lock reproduces the SQLite guarantee
# exactly, and write volume is low enough (ingest is a batch job) that
# serializing writers costs nothing that matters.

_PG_DDL = (
    "CREATE TABLE IF NOT EXISTS students ("
    " cas_id TEXT PRIMARY KEY,"
    " record TEXT NOT NULL,"
    " name   TEXT,"
    " index_json TEXT,"
    " updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS jobs ("
    " id TEXT PRIMARY KEY,"
    " state TEXT NOT NULL,"
    " updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS quarantine ("
    " id BIGSERIAL PRIMARY KEY,"
    " source_row INTEGER,"
    " cas_id TEXT,"
    " errors TEXT,"
    " raw TEXT,"
    " created_at TEXT)",
    # index() and all() order by name; without this it's a sort of every row.
    "CREATE INDEX IF NOT EXISTS students_name_lower ON students (LOWER(name))",
)

#: Arbitrary constant, only has to be stable across instances — it's the key
#: every writer contends on.
_PG_WRITE_LOCK = 8_675_309


class _PgCursor:
    """A psycopg2 cursor that answers to sqlite3.Row's subscripting."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _PgConn:
    """psycopg2 connection wearing sqlite3.Connection's interface.

    Lets every query body in Store stay backend-agnostic: `conn.execute(sql,
    params)` returning something you can fetch from. Placeholders are written
    as `?` throughout and rewritten here, so there is one SQL string per query
    rather than two that can drift apart.
    """

    def __init__(self, raw):
        self._raw = raw
        self.in_transaction = False

    def execute(self, sql: str, params: tuple = ()):
        import psycopg2.extras
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        upper = sql.strip().upper()
        if upper.startswith("BEGIN"):
            self.in_transaction = True
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_WRITE_LOCK,))
            return _PgCursor(cur)
        if upper.startswith(("COMMIT", "ROLLBACK")):
            self._raw.commit() if upper.startswith("COMMIT") else self._raw.rollback()
            self.in_transaction = False
            return _PgCursor(cur)
        cur.execute(sql.replace("?", "%s"), params)
        return _PgCursor(cur)

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


class Store:
    """Transactional store for unified student records + quarantine rows."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.is_pg = is_postgres(db_path)
        if self.is_pg:
            # No filesystem probing: the whole WAL / OS-lock / relocate dance
            # below exists because SQLite is a file, and a server isn't.
            with _CONFIG_GUARD:
                if db_path not in _DB_CONFIG:
                    _DB_LOCKS[db_path] = threading.RLock()
                    _DB_CONFIG[db_path] = self._init_pg()
            self._cfg = _DB_CONFIG[db_path]
            self._lock = _DB_LOCKS[db_path]
            return
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with _CONFIG_GUARD:
            if db_path not in _DB_CONFIG:
                _DB_LOCKS[db_path] = threading.RLock()
                _DB_CONFIG[db_path] = self._init_db()
        self._cfg = _DB_CONFIG[db_path]
        self._lock = _DB_LOCKS[db_path]
        # use the effective path (may have been relocated off a hostile mount)
        self.db_path = self._cfg.get("path", db_path)

    # ---- dialect --------------------------------------------------------
    @property
    def _nocase(self) -> str:
        """Case-insensitive ordering by name."""
        return "LOWER(name)" if self.is_pg else "name COLLATE NOCASE"

    @property
    def _sketch_expr(self) -> str:
        """Pull one field out of the cached index without parsing the rest."""
        return ("index_json::jsonb ->> 'sop_sketch'" if self.is_pg
                else "json_extract(index_json, '$.sop_sketch')")

    def _init_pg(self) -> Dict[str, Any]:
        conn = self._pg_connect()
        try:
            for ddl in _PG_DDL:
                conn.execute(ddl)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return {"journal": "postgres", "nolock": False, "path": self.db_path}

    def _pg_connect(self) -> "_PgConn":
        import psycopg2
        return _PgConn(psycopg2.connect(self.db_path, connect_timeout=15))

    # ---- connection -----------------------------------------------------
    def _open(self, *, nolock: bool, path: Optional[str] = None) -> sqlite3.Connection:
        path = path or self.db_path
        if nolock:
            uri = "file:" + quote(os.path.abspath(path)) + "?nolock=1"
            conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        else:
            conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")   # wait, don't fail, on a lock
        return conn

    def _connect(self):
        if self.is_pg:
            return self._pg_connect()
        return self._open(nolock=self._cfg["nolock"])

    @staticmethod
    def _probe_txn(conn: sqlite3.Connection) -> None:
        """Confirm the filesystem supports a real OS-locked transactional write."""
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS _ptx (x)")
        conn.execute("INSERT INTO _ptx(x) VALUES (1)")
        conn.execute("COMMIT")
        conn.execute("DROP TABLE _ptx")

    @staticmethod
    def _remove_files(path: str) -> None:
        """Delete a database and all its sidecars (only for throwaway probe files)."""
        for ext in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(path + ext)
            except OSError:
                pass

    @staticmethod
    def _remove_sidecars(path: str) -> None:
        """Delete only the journal/WAL sidecars (never the database itself), to
        clear a stale hot-journal left by a crashed write. Safe for data."""
        for ext in ("-wal", "-shm", "-journal"):
            try:
                os.remove(path + ext)
            except OSError:
                pass

    def _open_nolock_off(self, path: str) -> None:
        """Create/verify the schema with nolock + no journal. Never deletes the
        database file, so existing records are preserved."""
        conn = self._open(nolock=True, path=path)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            _apply_ddl(conn)
        finally:
            conn.close()

    def _relocated_path(self) -> str:
        """A stable local fallback path used only when the target directory's
        filesystem cannot host SQLite at all (no locks AND no deletes — e.g. some
        FUSE/synced mounts). Keyed to the original path so it's deterministic."""
        import hashlib
        import tempfile
        key = hashlib.sha1(os.path.abspath(self.db_path).encode()).hexdigest()[:16]
        d = os.path.join(tempfile.gettempdir(), "gradapp-store")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"students-{key}.db")

    def _fs_supports_locks(self) -> bool:
        """Probe OS-lock support on a THROWAWAY sibling file, so the real database
        is only ever opened in the final chosen mode (a failed lock probe on the
        real file can leave FUSE/network mounts in a state that then rejects even
        the nolock fallback)."""
        # Fixed name (not per-PID) so a filesystem that forbids deletes can't
        # accumulate probe files — at most one leftover.
        probe = f"{self.db_path}.locktest"
        conn = self._open(nolock=False, path=probe)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            self._probe_txn(conn)
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            for ext in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(probe + ext)
                except OSError:
                    pass

    def _init_db(self) -> Dict[str, Any]:
        if self._fs_supports_locks():
            # Locks work. Prefer WAL (live readers during a write); if WAL itself
            # isn't supported, fall back to the DELETE journal we just verified.
            conn = self._open(nolock=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                self._probe_txn(conn)
                _apply_ddl(conn)
                return {"journal": "wal", "nolock": False, "path": self.db_path}
            except sqlite3.OperationalError:
                pass
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            conn = self._open(nolock=False)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                _apply_ddl(conn)
                return {"journal": "delete", "nolock": False, "path": self.db_path}
            finally:
                conn.close()

        # No OS locks (e.g. bindfs/network/FUSE mounts): nolock + no journal, with
        # the per-path in-process lock providing serialization. Try the target path
        # first; if that filesystem also can't host SQLite (undeletable stale
        # journal, etc.), relocate to a local working directory. The database file
        # is NEVER deleted here — existing records are preserved.
        for path in (self.db_path, self._relocated_path()):
            for attempt in (1, 2):
                try:
                    self._open_nolock_off(path)
                    if path != self.db_path:
                        sys.stderr.write(
                            f"[store] '{self.db_path}' can't host SQLite on this "
                            f"filesystem (no OS locks/deletes); using a local "
                            f"database at {path} instead.\n")
                    return {"journal": "off", "nolock": True, "path": path}
                except sqlite3.OperationalError:
                    if attempt == 1:
                        self._remove_sidecars(path)    # clear a stale hot journal, keep data
                    # else: fall through to the next candidate path
        raise sqlite3.OperationalError(
            "could not initialize the student store on this filesystem")

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """Run one write transaction under the per-path in-process lock.

        The lock serializes writers within this process (the safety net when the
        filesystem can't provide OS locks); ``BEGIN IMMEDIATE`` adds cross-process
        serialization on filesystems that can. Rollback is guarded so a failure
        never masks the original error.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                raise
            finally:
                conn.close()

    # ---- single-record access ------------------------------------------
    def get(self, cas_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT record FROM students WHERE cas_id=?",
                               (cas_id,)).fetchone()
            return json.loads(row["record"]) if row else None
        finally:
            conn.close()

    def put(self, unified: Dict[str, Any]) -> None:
        """Insert/replace a whole record atomically (single transaction)."""
        with self._writing() as conn:
            self._write(conn, unified)

    def update(self, cas_id: str, mutate: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
               *, create: bool = True) -> Optional[Dict[str, Any]]:
        """Atomic read-modify-write for one student.

        Loads the current record (or a fresh one if absent and ``create``), hands
        it to ``mutate``, and writes the result back — all in one locked write
        transaction, so concurrent writers to the same ``cas_id`` serialize
        instead of overwriting each other. ``mutate`` may modify in place and/or
        return the record.
        """
        with self._writing() as conn:
            row = conn.execute("SELECT record FROM students WHERE cas_id=?",
                               (cas_id,)).fetchone()
            if row:
                unified = json.loads(row["record"])
            elif create:
                unified = new_student(cas_id)
            else:
                return None
            unified = mutate(unified) or unified
            self._write(conn, unified)
            return unified

    @staticmethod
    def _index_entry(unified: Dict[str, Any]) -> Dict[str, Any]:
        """The lightweight list-view entry for one student (no document text).

        The undergrad degree is derived here, once per write, and cached — it
        means re-reading the résumé and transcript text, and doing that for
        every applicant on every page view made the status export take eleven
        seconds at five thousand records.
        """
        app = unified["sources"].get("application") or {}
        sources = unified.get("sources") or {}
        return {
            "cas_id": unified["student_id"],
            "name": unified["identity"].get("full_name", unified["student_id"]),
            "email": unified["identity"].get("email", ""),
            "undergrad_degree": undergrad_degree(unified),
            "has_packet": any(sources.get(k) for k in ("resume", "sop", "scores"))
                          or bool(sources.get("lors")),
            # Which roster file this applicant came from, so the review-sheet
            # writer can find it without reading every full record's provenance.
            "source_file": next((p.get("file") for p in reversed(unified.get("provenance") or [])
                                 if p.get("source") == "application" and p.get("file")), ""),
            # Sketch of this applicant's SOP, so the near-duplicate check can
            # shortlist candidates from the index instead of reading every
            # record's full essay for every incoming document.
            "sop_sketch": sketch((sources.get("sop") or {}).get("text") or ""),
            "programs": [p.get("name") for p in app.get("programs", []) if p.get("name")],
            "sources_present": sources_present(unified),
            "warnings": unified.get("warnings", []),
            "ocr": unified.get("ocr"),
            "summary_text": unified.get("summary", ""),
            "review_status": unified.get("review_status", ""),
            "triage": unified.get("triage", ""),
            "assigned_to": unified.get("assigned_to", ""),
            "professor_status": unified.get("professor_status", ""),
            "professor_decided_at": unified.get("professor_decided_at", ""),
            "metrics": applicant_metrics(unified),
        }

    @staticmethod
    def _write(conn: sqlite3.Connection, unified: Dict[str, Any]) -> None:
        cas_id = unified["student_id"]
        name = (unified.get("identity") or {}).get("full_name") or cas_id
        idx = json.dumps(Store._index_entry(unified), ensure_ascii=False)
        conn.execute(
            "INSERT INTO students(cas_id, record, name, index_json, updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(cas_id) DO UPDATE SET "
            " record=excluded.record, name=excluded.name,"
            " index_json=excluded.index_json, updated_at=excluded.updated_at",
            (cas_id, json.dumps(unified, ensure_ascii=False), name, idx, _now()))

    def delete(self, cas_id: str) -> None:
        with self._writing() as conn:
            conn.execute("DELETE FROM students WHERE cas_id=?", (cas_id,))

    # ---- collections ----------------------------------------------------
    def all(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT record FROM students ORDER BY {self._nocase}").fetchall()
            return [json.loads(r["record"]) for r in rows]
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
        finally:
            conn.close()

    def sop_sketches(self) -> List[tuple]:
        """[(cas_id, sketch)] for everyone with an SOP, and nothing else.

        The near-duplicate check runs once per incoming document against every
        applicant on file. Going through `index()` for that pulled each
        applicant's whole summary card through JSON for every comparison; this
        pulls two small fields with SQLite doing the extraction.
        """
        conn = self._connect()
        try:
            try:
                rows = conn.execute(
                    f"SELECT cas_id, {self._sketch_expr} AS sk "
                    "FROM students WHERE index_json IS NOT NULL").fetchall()
                # An applicant with no SOP still has a sop_sketch — it's just
                # `[]`. Dropping those here keeps the near-duplicate scan
                # proportional to the essays on file rather than the roster.
                out = []
                for r in rows:
                    sk = json.loads(r["sk"]) if r["sk"] and r["sk"] != "null" else None
                    if sk:
                        out.append((r["cas_id"], sk))
                return out
            except sqlite3.OperationalError:
                # No JSON1 in this SQLite build — fall back to parsing here.
                rows = conn.execute(
                    "SELECT cas_id, index_json FROM students "
                    "WHERE index_json IS NOT NULL").fetchall()
                out = []
                for r in rows:
                    sk = (json.loads(r["index_json"]) or {}).get("sop_sketch")
                    if sk:
                        out.append((r["cas_id"], sk))
                return out
        finally:
            conn.close()

    def index(self) -> List[Dict[str, Any]]:
        """The lightweight per-student index the UI/list view consumes.

        Served from the cached index_json column, so listing hundreds of
        applicants never deserializes full records (with complete SOP/LOR
        text). Rows written before the cache existed fall back to computing
        the entry from the full record."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT record, index_json FROM students "
                f"ORDER BY {self._nocase}").fetchall()
        finally:
            conn.close()
        out: List[Dict[str, Any]] = []
        for r in rows:
            if r["index_json"]:
                out.append(json.loads(r["index_json"]))
            else:   # pre-migration row: compute from the full record
                out.append(self._index_entry(json.loads(r["record"])))
        return out

    def clear_students(self) -> int:
        with self._writing() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
            conn.execute("DELETE FROM students")
            return n

    # ---- quarantine -----------------------------------------------------
    def replace_quarantine(self, entries: List[Dict[str, Any]]) -> None:
        """Overwrite the quarantine set (one CSV run's invalid rows)."""
        with self._writing() as conn:
            conn.execute("DELETE FROM quarantine")
            for e in entries:
                conn.execute(
                    "INSERT INTO quarantine(source_row, cas_id, errors, raw, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (e.get("source_row"), e.get("cas_id"),
                     json.dumps(e.get("errors"), ensure_ascii=False),
                     json.dumps(e.get("raw"), ensure_ascii=False) if "raw" in e else None,
                     _now()))

    def quarantine(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT source_row, cas_id, errors, raw FROM quarantine ORDER BY id").fetchall()
            out = []
            for r in rows:
                e = {"source_row": r["source_row"], "cas_id": r["cas_id"],
                     "errors": json.loads(r["errors"]) if r["errors"] else None}
                if r["raw"] is not None:
                    e["raw"] = json.loads(r["raw"])
                out.append(e)
            return out
        finally:
            conn.close()

    def clear_quarantine(self) -> None:
        with self._writing() as conn:
            conn.execute("DELETE FROM quarantine")

    # ---- jobs -----------------------------------------------------------
    #
    # Only here so a progress poll that lands on a different instance than the
    # one doing the work can still answer. Deliberately a dumb key/value blob:
    # nothing queries inside it, and giving it real columns would invite
    # someone to think this is a job queue. It isn't — see src/jobs.py.

    def put_job(self, job_id: str, state: str) -> None:
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO jobs(id, state, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " state=excluded.state, updated_at=excluded.updated_at",
                (job_id, state, _now()))

    def get_job(self, job_id: str) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT state FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
            return row["state"] if row else None
        finally:
            conn.close()

    def update_job(self, job_id: str,
                   mutate: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]]
                   ) -> Dict[str, Any]:
        """Atomic read-modify-write of one job's state.

        Needed because with a queue the units of a job finish on whatever
        instances happen to pick them up, and each one increments the same
        counter. Two workers finishing at the same moment would otherwise both
        read 4, both write 5, and the job would never reach its total — a
        progress bar stuck one short of done, forever.
        """
        with self._writing() as conn:
            row = conn.execute("SELECT state FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
            current = json.loads(row["state"]) if row else None
            updated = mutate(current)
            conn.execute(
                "INSERT INTO jobs(id, state, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " state=excluded.state, updated_at=excluded.updated_at",
                (job_id, json.dumps(updated, ensure_ascii=False), _now()))
            return updated

    def prune_jobs(self, keep: int = 200) -> None:
        """Bounded, so a long-lived server doesn't accumulate them forever."""
        with self._writing() as conn:
            conn.execute(
                "DELETE FROM jobs WHERE id NOT IN "
                "(SELECT id FROM jobs ORDER BY updated_at DESC LIMIT ?)", (keep,))

    # ---- reconcile ------------------------------------------------------
    def reconcile(self) -> int:
        """Fold same-person records keyed differently (email/ROW vs numeric
        cas_id) into the cas_id-keyed record. Returns how many were merged away.

        Conservative: only collapses a non-cas_id key into a matching cas_id key
        when their names agree; never merges two distinct cas_ids. Runs in one
        locked write transaction so it is atomic w.r.t. other writers.
        """
        with self._writing() as conn:
            rows = conn.execute("SELECT cas_id, record FROM students").fetchall()
            recs = {r["cas_id"]: json.loads(r["record"]) for r in rows}

            groups: Dict[str, List[str]] = {}
            for sid, u in recs.items():
                nm = _norm_name(u.get("identity", {}).get("full_name") or sid)
                if nm:
                    groups.setdefault(nm, []).append(sid)

            merged_away: List[str] = []
            for nm, keys in groups.items():
                if len(keys) < 2:
                    continue
                cas_keys = [k for k in keys if _is_cas_id(k)]
                other_keys = [k for k in keys if not _is_cas_id(k)]
                if not cas_keys or not other_keys:
                    continue
                target = recs[cas_keys[0]]
                for ok in other_keys:
                    ou = recs[ok]
                    # Same name is not enough when both records carry an email:
                    # two distinct applicants can share a name. Merge only if
                    # the emails agree (or at least one side has none).
                    te = (target.get("identity", {}).get("email") or "").strip().lower()
                    oe = (ou.get("identity", {}).get("email") or "").strip().lower()
                    if te and oe and te != oe:
                        continue
                    merge_identity(target["identity"], ou.get("identity", {}))
                    for stype in ("application", "resume", "sop"):
                        if ou["sources"].get(stype) and not target["sources"].get(stype):
                            target["sources"][stype] = ou["sources"][stype]
                    target["sources"]["lors"].extend(ou["sources"].get("lors") or [])
                    target["provenance"].extend(ou.get("provenance", []))
                    for w in ou.get("warnings", []):
                        if w not in target["warnings"]:
                            target["warnings"].append(w)
                    if ou.get("ocr") and not target.get("ocr"):
                        target["ocr"] = ou["ocr"]
                    # A review status recorded against the duplicate is the
                    # reviewer's work — carry it over rather than dropping it
                    # and silently returning the applicant to the tbd pile.
                    if ou.get("review_status") and not target.get("review_status"):
                        target["review_status"] = ou["review_status"]
                    merged_away.append(ok)
                target["summary"] = render(summary_view(target))
                self._write(conn, target)

            for ok in set(merged_away):
                conn.execute("DELETE FROM students WHERE cas_id=?", (ok,))
            return len(set(merged_away))
