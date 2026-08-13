"""Where uploaded and generated files live.

On a laptop that's a directory and this module is barely there. On Cloud Run it
can't be: the container's disk is per-instance and wiped on restart, so a roster
uploaded through one instance is invisible to the next request and gone after a
redeploy.

**Sync-through, not replace.** The obvious design — pass bytes around instead of
paths — founders on the fact that most of the pipeline hands real file paths to
libraries that want real files (pandas, openpyxl, pypdf, pypdfium2, Tesseract).
Rewriting all of that to work on streams would be a large change to code that is
correct today, for no benefit on the local path.

So the local directory stays exactly as it is and becomes a cache over the
bucket:

    write  -> local disk, then mirrored to the bucket
    read   -> if it isn't on this instance's disk, fetch it first
    delete -> both

Every existing path in the app keeps working unchanged; what changes is that a
file written by one instance can be found by another. The cost is a copy of each
file in the container's ephemeral storage, which is fine for the sizes involved
(a roster and a batch of packets) and is what OCR needs anyway.

Selected by GRADAPP_BUCKET, the same way the database is selected by
GRADAPP_DATABASE_URL: set it and the app is backed by Cloud Storage, leave it
unset and nothing about local behaviour changes.
"""
from __future__ import annotations

import os
import threading
from typing import Optional


def _rel(root: str, path: str) -> str:
    """Bucket key for a path under the data root, with posix separators."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return rel.replace(os.sep, "/")


class LocalBlobs:
    """The laptop. Files are just files; every method is a thin wrapper so the
    call sites don't have to know which backend they got."""

    remote = False

    def __init__(self, root: str):
        self.root = root

    def save_upload(self, file, dest: str) -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        file.save(dest)

    def publish(self, path: str) -> None:
        """Called after something writes `path` directly. Nothing to do here."""

    def publish_tree(self, directory: str) -> None:
        """Same, for a directory something has just filled."""

    def ensure(self, path: str) -> bool:
        return os.path.exists(path)

    def ensure_tree(self, directory: str) -> None:
        """Make sure every file under `directory` is present locally."""

    def remove(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def remove_tree(self, directory: str) -> None:
        for root, _, files in os.walk(directory):
            for name in files:
                self.remove(os.path.join(root, name))


class GcsBlobs(LocalBlobs):
    """Cloud Storage behind the same interface."""

    remote = True

    def __init__(self, root: str, bucket: str):
        super().__init__(root)
        self.bucket_name = bucket
        self._bucket = None
        self._lock = threading.Lock()

    # -- connection ------------------------------------------------------
    @property
    def bucket(self):
        # Lazy: a local checkout never installs google-cloud-storage, and
        # importing it at module scope would make src.app unimportable there.
        if self._bucket is None:
            with self._lock:
                if self._bucket is None:
                    from google.cloud import storage
                    self._bucket = storage.Client().bucket(self.bucket_name)
        return self._bucket

    def _blob(self, path: str):
        return self.bucket.blob(_rel(self.root, path))

    # -- writes ----------------------------------------------------------
    def save_upload(self, file, dest: str) -> None:
        super().save_upload(file, dest)
        self.publish(dest)

    def publish(self, path: str) -> None:
        if os.path.exists(path):
            self._blob(path).upload_from_filename(path)

    def publish_tree(self, directory: str) -> None:
        for root, _, files in os.walk(directory):
            for name in files:
                self.publish(os.path.join(root, name))

    # -- reads -----------------------------------------------------------
    def ensure(self, path: str) -> bool:
        """True if the file is now on local disk, fetching it if it wasn't.

        The local check comes first so a file this instance already has costs
        nothing — the common case is that the instance handling the download is
        the one that just did the upload.
        """
        if os.path.exists(path):
            return True
        blob = self._blob(path)
        if not blob.exists():
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        blob.download_to_filename(path)
        return True

    def ensure_tree(self, directory: str) -> None:
        prefix = _rel(self.root, directory).rstrip("/") + "/"
        for blob in self.bucket.list_blobs(prefix=prefix):
            local = os.path.join(self.root, blob.name.replace("/", os.sep))
            if not os.path.exists(local):
                os.makedirs(os.path.dirname(local), exist_ok=True)
                blob.download_to_filename(local)

    # -- deletes ---------------------------------------------------------
    def remove(self, path: str) -> None:
        super().remove(path)
        try:
            self._blob(path).delete()
        except Exception:
            pass          # already gone is the outcome we wanted

    def remove_tree(self, directory: str) -> None:
        super().remove_tree(directory)
        prefix = _rel(self.root, directory).rstrip("/") + "/"
        try:
            for blob in self.bucket.list_blobs(prefix=prefix):
                blob.delete()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Runtime-written config (config/users.yaml, config/professors.yaml)
#
# These look like static config and aren't: the app writes both while running —
# approving an account, adding a professor. In a container they sit on the
# instance's own disk, so without this the advisor account created at /setup
# would disappear on the next deploy, and a faculty entry added on one instance
# would be invisible to the others. Both failures look like the app forgetting
# things at random.
#
# Same sync-through trick as everything else, exposed as two functions because
# users.py and triage.py shouldn't have to know a bucket exists.
# --------------------------------------------------------------------------

_CONFIG_BLOBS = None


def use_for_config(store) -> None:
    """Make runtime-written config files durable through `store`."""
    global _CONFIG_BLOBS
    _CONFIG_BLOBS = store


def pull_config(path: str) -> None:
    """Fetch this file from the bucket if the instance doesn't have it."""
    if _CONFIG_BLOBS is not None and _CONFIG_BLOBS.remote:
        try:
            _CONFIG_BLOBS.ensure(path)
        except Exception:
            pass          # a missing bucket must not make the app unreadable


def push_config(path: str) -> None:
    """Mirror this file to the bucket after writing it."""
    if _CONFIG_BLOBS is not None and _CONFIG_BLOBS.remote:
        try:
            _CONFIG_BLOBS.publish(path)
        except Exception:
            pass


_CACHE: dict = {}
_GUARD = threading.Lock()


def blobs_for(root: str, bucket: Optional[str] = None) -> LocalBlobs:
    """The store for this data directory. One per (root, bucket) per process."""
    bucket = bucket if bucket is not None else os.environ.get("GRADAPP_BUCKET", "").strip()
    key = (os.path.abspath(root), bucket)
    if key not in _CACHE:
        with _GUARD:
            if key not in _CACHE:
                _CACHE[key] = GcsBlobs(root, bucket) if bucket else LocalBlobs(root)
    return _CACHE[key]
