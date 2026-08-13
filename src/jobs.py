"""In-process background jobs, so a long ingest can report progress.

Reading a batch of scanned packets takes minutes — OCR is the slow part — and a
browser sitting on a blank POST for that long looks broken. A job runs on a
worker thread while the page polls `progress()` and shows what's being read now.

Deliberately tiny: one process, one dict, no queue and no persistence. This is a
single-reviewer local tool; a job that dies with the process is a job whose work
the user will just run again. Anything durable belongs in the store, and the
ingest functions already write there as they go, so a crashed job leaves the
records it already merged rather than nothing.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

# job_id -> state. Capped so a long-lived server can't grow unboundedly.
_JOBS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_MAX_JOBS = 40


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
                         "result": None, "error": None}
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


def get(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _reset_for_tests() -> None:
    with _LOCK:
        _JOBS.clear()
