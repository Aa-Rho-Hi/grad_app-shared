"""The two things that only break once there's more than one instance.

Both failures are invisible on a laptop, because a laptop is one process with
one disk. They're also both silent rather than loud in production: a download
404s, or a progress bar sits at zero while the work is actually fine. So these
tests simulate the second instance explicitly — a fresh local directory sharing
one bucket, and a poller that never saw the job start.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import jobs
from src.storage import GcsBlobs, LocalBlobs, blobs_for
from src.store import Store, db_path_for


# --------------------------------------------------------------------------
# A stand-in for a GCS bucket. Small enough to read, faithful on the parts the
# code actually leans on: keys are flat strings, exists() is a real check, and
# listing is prefix-based.
# --------------------------------------------------------------------------
class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_filename(self, path):
        with open(path, "rb") as fh:
            self.bucket.data[self.name] = fh.read()

    def download_to_filename(self, path):
        with open(path, "wb") as fh:
            fh.write(self.bucket.data[self.name])

    def exists(self):
        return self.name in self.bucket.data

    def delete(self):
        self.bucket.data.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.data = {}

    def blob(self, name):
        return FakeBlob(self, name)

    def list_blobs(self, prefix=""):
        return [FakeBlob(self, n) for n in sorted(self.data) if n.startswith(prefix)]


def _gcs(root, bucket):
    store = GcsBlobs(str(root), "test-bucket")
    store._bucket = bucket
    return store


class _Upload:
    """Enough of a Werkzeug FileStorage for save_upload()."""

    def __init__(self, data):
        self.data = data

    def save(self, dest):
        with open(dest, "wb") as fh:
            fh.write(self.data)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def test_a_file_uploaded_to_one_instance_is_readable_from_another(tmp_path):
    bucket = FakeBucket()
    first, second = tmp_path / "instance-a", tmp_path / "instance-b"
    (first / "raw").mkdir(parents=True)
    (second / "raw").mkdir(parents=True)

    a = _gcs(first, bucket)
    a.save_upload(_Upload(b"cas_id,first_name\n1,Ada\n"), str(first / "raw" / "roster.csv"))

    # the second instance has never seen it
    b = _gcs(second, bucket)
    target = str(second / "raw" / "roster.csv")
    assert not os.path.exists(target)
    assert b.ensure(target) is True
    assert open(target, "rb").read().startswith(b"cas_id")

    # and something genuinely absent is still absent
    assert b.ensure(str(second / "raw" / "nope.csv")) is False


def test_a_report_built_on_one_instance_downloads_from_another(tmp_path):
    bucket = FakeBucket()
    first, second = tmp_path / "a", tmp_path / "b"
    (first / "reports").mkdir(parents=True)
    (second / "reports").mkdir(parents=True)

    built = first / "reports" / "applicants_review.xlsx"
    built.write_bytes(b"PK\x03\x04 pretend workbook")
    _gcs(first, bucket).publish(str(built))

    assert _gcs(second, bucket).ensure(str(second / "reports" / "applicants_review.xlsx"))


def test_ensure_tree_pulls_the_whole_upload_folder(tmp_path):
    """The review sheet reads the advisor's original roster, which may have
    been uploaded through a different instance."""
    bucket = FakeBucket()
    first, second = tmp_path / "a", tmp_path / "b"
    (first / "raw" / "packets").mkdir(parents=True)
    (second).mkdir(parents=True, exist_ok=True)

    a = _gcs(first, bucket)
    for name, body in [("raw/roster.csv", b"a,b\n"), ("raw/packets/p1.pdf", b"%PDF-1"),
                       ("raw/packets/p2.pdf", b"%PDF-2")]:
        path = first / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        a.publish(str(path))

    _gcs(second, bucket).ensure_tree(str(second / "raw"))
    assert (second / "raw" / "roster.csv").exists()
    assert (second / "raw" / "packets" / "p2.pdf").read_bytes() == b"%PDF-2"


def test_purging_raw_uploads_clears_the_bucket_too(tmp_path):
    """'Also delete raw uploads' has to mean it. Deleting the local copy while
    every applicant's PDF stays in Cloud Storage is the worst outcome here."""
    bucket = FakeBucket()
    root = tmp_path / "a"
    (root / "raw").mkdir(parents=True)
    store = _gcs(root, bucket)
    (root / "raw" / "packet.pdf").write_bytes(b"%PDF applicant")
    store.publish(str(root / "raw" / "packet.pdf"))
    assert bucket.data

    store.remove_tree(str(root / "raw"))
    assert not bucket.data, "applicant files survived in the bucket"
    assert not (root / "raw" / "packet.pdf").exists()


def test_local_mode_is_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv("GRADAPP_BUCKET", raising=False)
    store = blobs_for(str(tmp_path))
    assert isinstance(store, LocalBlobs) and not store.remote

    (tmp_path / "raw").mkdir()
    dest = str(tmp_path / "raw" / "x.csv")
    store.save_upload(_Upload(b"hello"), dest)
    assert open(dest, "rb").read() == b"hello"
    assert store.ensure(dest) is True
    assert store.ensure(str(tmp_path / "raw" / "missing.csv")) is False


def test_accounts_survive_an_instance_being_replaced(tmp_path, monkeypatch):
    """users.yaml looks like static config and isn't — the app writes it when
    an advisor is created or approved. In a container it lives on the image's
    own disk, so without mirroring, the account created at /setup is gone on
    the next deploy and nobody can sign in."""
    from src import storage
    from src import users as usersmod

    bucket = FakeBucket()
    first, second = tmp_path / "a", tmp_path / "b"
    (first / "config").mkdir(parents=True)
    (second / "config").mkdir(parents=True)

    # instance A: an advisor claims the app
    storage.use_for_config(_gcs(first, bucket))
    monkeypatch.setattr(usersmod, "USERS_YAML", str(first / "config" / "users.yaml"))
    usersmod.save_users(usersmod.add_user([], "maya", "a-long-password",
                                          usersmod.ADVISOR))

    # instance B: fresh disk, has never seen that file
    storage.use_for_config(_gcs(second, bucket))
    monkeypatch.setattr(usersmod, "USERS_YAML", str(second / "config" / "users.yaml"))
    assert [u["username"] for u in usersmod.load_users()] == ["maya"]
    assert usersmod.authenticate("maya", "a-long-password") is not None
    storage.use_for_config(None)


def test_the_faculty_list_survives_too(tmp_path, monkeypatch):
    from src import storage
    from src import triage as triagemod

    bucket = FakeBucket()
    first, second = tmp_path / "a", tmp_path / "b"
    (first / "config").mkdir(parents=True)
    (second / "config").mkdir(parents=True)

    storage.use_for_config(_gcs(first, bucket))
    monkeypatch.setattr(triagemod, "PROFESSORS_YAML",
                        str(first / "config" / "professors.yaml"))
    triagemod.save_professors([{"id": "gratz-paul", "name": "Paul Gratz"}])

    storage.use_for_config(_gcs(second, bucket))
    monkeypatch.setattr(triagemod, "PROFESSORS_YAML",
                        str(second / "config" / "professors.yaml"))
    assert [p["name"] for p in triagemod.load_professors()] == ["Paul Gratz"]
    storage.use_for_config(None)


def test_config_mirroring_is_off_locally(tmp_path, monkeypatch):
    """A laptop must not start reaching for a bucket that isn't there."""
    from src import storage
    from src import users as usersmod

    storage.use_for_config(LocalBlobs(str(tmp_path)))
    monkeypatch.setattr(usersmod, "USERS_YAML", str(tmp_path / "users.yaml"))
    usersmod.save_users(usersmod.add_user([], "solo", "a-long-password",
                                          usersmod.ADVISOR))
    assert [u["username"] for u in usersmod.load_users()] == ["solo"]
    storage.use_for_config(None)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

@pytest.fixture
def shared_store(tmp_path):
    store = Store(db_path_for(str(tmp_path)))
    jobs.use_store(lambda: store)
    jobs._reset_for_tests()
    yield store
    jobs.use_store(None)
    jobs._reset_for_tests()


def test_progress_is_readable_from_an_instance_that_never_started_the_job(shared_store):
    """The hang: the browser polls /jobs/<id>, the request is load-balanced to
    an instance with an empty dict, and the bar sits at zero while the upload
    is in fact running."""
    done = []

    def work(progress):
        progress(3, 10, "reading packet 3 of 10")
        done.append(True)
        return {"message": "finished"}

    job_id = jobs.start(work, "Reading packets…")
    while not done:
        pass

    jobs._reset_for_tests()            # <- the other instance: knows nothing
    seen = jobs.get(job_id)
    assert seen is not None, "a second instance could not see the job at all"
    assert seen["total"] == 10 and seen["done_count"] == 3


def test_an_unknown_job_is_still_unknown(shared_store):
    assert jobs.get("neverexisted") is None


def test_a_job_whose_instance_vanished_stops_spinning(shared_store):
    """Nobody is going to update this row again, so the poller has to be the
    one to give up — and say what happened to the work already done."""
    stale = {"id": "ghost", "started": "2020-01-01T00:00:00+00:00",
             "heartbeat": "2020-01-01T00:00:00+00:00", "done": False,
             "label": "Reading packets…", "done_count": 2, "total": 40,
             "result": None, "error": None}
    shared_store.put_job("ghost", json.dumps(stale))

    seen = jobs.get("ghost")
    assert seen["done"] is True, "the browser would poll this forever"
    assert "already read has been saved" in seen["error"]

    # a finished job is never called stale, however old
    finished = {**stale, "id": "old", "done": True, "error": None}
    shared_store.put_job("old", json.dumps(finished))
    assert jobs.get("old")["error"] is None


def test_the_local_copy_wins_while_the_job_is_running(shared_store):
    """The dict is authoritative for the instance doing the work; the shared row
    is only as fresh as the last heartbeat."""
    # Seeded rather than run: a real worker races us to the finish line and
    # overwrites the label with "Done" before the assertion.
    live = {"id": "running", "started": jobs._now(), "heartbeat": jobs._now(),
            "done": False, "label": "fresher", "done_count": 7, "total": 10,
            "result": None, "error": None}
    jobs._JOBS["running"] = live
    shared_store.put_job("running", json.dumps({**live, "label": "staler",
                                                "done_count": 2}))
    seen = jobs.get("running")
    assert seen["label"] == "fresher" and seen["done_count"] == 7


def test_jobs_are_pruned(shared_store):
    for i in range(12):
        shared_store.put_job(f"j{i}", json.dumps({"id": f"j{i}", "done": True}))
    shared_store.prune_jobs(keep=5)
    kept = [i for i in range(12) if shared_store.get_job(f"j{i}")]
    assert len(kept) == 5
