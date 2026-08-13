"""In-process background jobs, so a long ingest can report progress.

Reading a batch of scanned packets takes minutes — OCR is the slow part — and a
browser sitting on a blank POST for that long looks broken. A job runs on a
worker thread while the page polls `progress()` and shows what's being read now.

On one machine that's a dict: a job that dies with the process is a job whose
work the user will just run again, and the ingest functions write to the store
as they go, so a crash leaves the records already merged rather than nothing.

Across several instances the dict alone is wrong, and wrong in a way that looks
like a hang. The browser polls `/jobs/<id>`; that request is load-balanced, so
it lands on an instance that never started the job, finds nothing, and the
progress bar sits at zero forever while the upload is in fact working. So when
a shared store is configured, state is mirrored into a `jobs` table and reads
fall back to it. The dict stays as the fast path for the instance actually
running the work.

What this does **not** do is move the work. The thread still runs on whichever
instance received the upload, so if that instance is recycled mid-ingest the job
stops — the poller then sees a job whose heartbeat has gone stale and says so,
instead of spinning. A real queue (Cloud Tasks, a worker service) is the fix for
that, and is worth doing if ingests ever get long enough for it to bite.
"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

# job_id -> state. Capped so a long-lived server can't grow unboundedly.
_JOBS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_MAX_JOBS = 40

#: Set by the app when a store is available, so jobs can be shared between
#: instances. Left None on a laptop, where the dict is the whole story.
_STORE_FACTORY: Optional[Callable[[], Any]] = None

#: How long a job can go without a heartbeat before a poller should assume the
#: instance running it is gone. Generous: OCR on a big packet is slow, and
#: calling a live job dead is worse than taking a while to notice a dead one.
STALE_AFTER = timedelta(minutes=5)


def use_store(factory: Optional[Callable[[], Any]]) -> None:
    """Share job state through this store, so any instance can report on it."""
    global _STORE_FACTORY
    _STORE_FACTORY = factory


def _shared():
    if _STORE_FACTORY is None:
        return None
    try:
        return _STORE_FACTORY()
    except Exception:
        return None            # never let bookkeeping break the actual work


def _persist(job: Dict[str, Any]) -> None:
    store = _shared()
    if store is None:
        return
    try:
        store.put_job(job["id"], json.dumps(job))
    except Exception:
        pass


def _load(job_id: str) -> Optional[Dict[str, Any]]:
    store = _shared()
    if store is None:
        return None
    try:
        raw = store.get_job(job_id)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Progress:
    """Handed to the worker so it can say where it's got to."""

    def __init__(self, job_id: str):
        self.job_id = job_id

    def __call__(self, done: int, total: int, label: str = "") -> None:
        # `done_count`, never `done`: the latter is the job's finished flag and
        # a per-item counter landing on it would end the job at item 1.
        self.update(done_count=done, total=total, label=label)

    def update(self, **fields: Any) -> None:
        with _LOCK:
            job = _JOBS.get(self.job_id)
            if job:
                job.update(fields)
                job["heartbeat"] = _now()
                snapshot = dict(job)
        if job:
            _persist(snapshot)

    def stage(self, label: str) -> None:
        """A phase change with no countable items yet (reading a spreadsheet)."""
        self.update(label=label, done_count=0, total=0)


def start(fn: Callable[[Progress], Any], label: str = "Working…") -> str:
    """Run `fn(progress)` on a worker thread. Returns the job id to poll."""
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        if len(_JOBS) >= _MAX_JOBS:            # drop the oldest finished jobs
            for old in sorted(_JOBS, key=lambda k: _JOBS[k]["started"]):
                if _JOBS[old]["done"]:
                    _JOBS.pop(old, None)
                if len(_JOBS) < _MAX_JOBS:
                    break
        _JOBS[job_id] = {"id": job_id, "started": _now(), "done": False,
                         "label": label, "done_count": 0, "total": 0,
                         "result": None, "error": None, "heartbeat": _now()}
        snapshot = dict(_JOBS[job_id])
    _persist(snapshot)
    progress = Progress(job_id)

    def _run():
        try:
            result = fn(progress)
            progress.update(result=result, error=None)
        except Exception as exc:                     # noqa: BLE001 - reported, not raised
            traceback.print_exc()
            progress.update(error=str(exc))
        finally:
            progress.update(done=True, label="Done")

    threading.Thread(target=_run, daemon=True, name=f"job-{job_id}").start()
    return job_id


def _is_stale(job: Dict[str, Any]) -> bool:
    beat = job.get("heartbeat") or job.get("started")
    if job.get("done") or not beat:
        return False
    try:
        last = datetime.fromisoformat(beat)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - last > STALE_AFTER


def get(job_id: str) -> Optional[Dict[str, Any]]:
    """This instance's copy if it has one, otherwise the shared one.

    The local dict wins because it's authoritative for a job this instance is
    running — the shared row is only as fresh as the last heartbeat.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        job = dict(job) if job else None
    if job is None:
        job = _load(job_id)
    if job and _is_stale(job):
        # Somebody has to tell the browser to stop spinning. The work may have
        # partly completed — ingest writes as it goes — so this says "lost",
        # not "failed", and the applicant list will show what did land.
        job = {**job, "done": True, "label": "Interrupted",
               "error": "The instance running this job stopped responding. "
                        "Anything already read has been saved — reload the "
                        "applicant list to see, then re-upload the rest."}
    return job


# --------------------------------------------------------------------------
# Fan-out jobs: one job, several units, each finished by whichever instance
# picked it up. Used when the work is queued rather than run in a thread.
# --------------------------------------------------------------------------

def open_fanout(total: int, label: str, next_url: str = "",
                notes: Optional[list] = None) -> str:
    """Register a job whose units will be completed elsewhere."""
    job_id = uuid.uuid4().hex[:12]
    state = {"id": job_id, "started": _now(), "heartbeat": _now(), "done": False,
             "label": label, "done_count": 0, "total": total,
             "result": None, "error": None, "messages": list(notes or []),
             "fanout": True}
    with _LOCK:
        _JOBS[job_id] = dict(state)
    _persist(state)
    return job_id


def finish_unit(job_id: str, message: str = "", next_url: str = "") -> Optional[Dict[str, Any]]:
    """Record one unit of a fan-out job as done. Returns the new state.

    Goes through the store's atomic read-modify-write, because units finish on
    whatever instances the queue hands them to. Two workers finishing at the
    same instant would otherwise both read the same counter and both write the
    same value, leaving the job permanently one short of its total.
    """
    store = _shared()

    def bump(state):
        state = state or {"id": job_id, "started": _now(), "done_count": 0,
                          "total": 1, "messages": [], "fanout": True}
        state["done_count"] = int(state.get("done_count") or 0) + 1
        state["heartbeat"] = _now()
        if message:
            state.setdefault("messages", []).append(message)
        if state["done_count"] >= int(state.get("total") or 0):
            state["done"] = True
            state["label"] = "Done"
            state["result"] = {"message": " ".join(state.get("messages") or []),
                               "next": next_url or state.get("next_url") or ""}
        else:
            state["label"] = f"Read {state['done_count']} of {state['total']}"
        return state

    if store is None:                      # no shared store: local dict only
        with _LOCK:
            current = _JOBS.get(job_id)
            updated = bump(dict(current) if current else None)
            _JOBS[job_id] = updated
        return updated
    try:
        updated = store.update_job(job_id, bump)
    except Exception:
        return None
    with _LOCK:                            # keep this instance's view fresh too
        _JOBS[job_id] = dict(updated)
    return updated


def _reset_for_tests() -> None:
    with _LOCK:
        _JOBS.clear()
