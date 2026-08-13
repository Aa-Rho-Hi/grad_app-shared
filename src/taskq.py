"""Handing ingest work to something that will retry it.

The problem this solves: an upload used to start a thread and return. On one
machine that's fine — if the process dies you were there, you saw it, you run it
again. On Cloud Run the container can be recycled at any moment (a deploy, a
scale-down, the platform moving you), and the reviewer sees a progress bar stop
with half their packets read and no indication which half.

So in cloud mode the work isn't done on the request at all. Each uploaded file
becomes a Cloud Task pointing at ``/tasks/run``; Cloud Tasks holds it until a
2xx comes back and redelivers otherwise. If the instance handling a task dies,
the task is still queued and some other instance picks it up.

Three things make that safe:

* **The file is already durable.** Uploads go to the bucket before anything is
  enqueued, so a worker on a different instance can fetch and read it.
* **One task per file, not per upload.** A batch of forty scanned packets as a
  single task would sit near the dispatch deadline and lose everything on a
  retry. Per file, each unit is small, and a redelivery repeats one file.
* **Ingest is idempotent.** Records merge by ``cas_id`` and sources are replaced
  per source file, so reading the same packet twice converges on the same
  record rather than duplicating it. Retries are therefore boring, which is the
  property you want from a queue.

Locally none of this exists and none of it is wanted: ``enabled()`` is False,
``enqueue`` returns False, and the caller runs the work on a thread exactly as
before.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

#: Cloud Tasks queue, e.g. "projects/p/locations/us-central1/queues/gradapp".
QUEUE = os.environ.get("GRADAPP_TASK_QUEUE", "").strip()
#: Base URL of this service, so a task knows where to call back.
BASE_URL = os.environ.get("GRADAPP_BASE_URL", "").strip().rstrip("/")
#: Service account Cloud Tasks signs the callback with; the worker verifies it.
INVOKER = os.environ.get("GRADAPP_TASK_INVOKER", "").strip()


def enabled() -> bool:
    """True when there's a real queue to hand work to."""
    return bool(QUEUE and BASE_URL)


def enqueue(payload: Dict[str, Any], *, path: str = "/tasks/run") -> bool:
    """Queue one unit of work. False means 'no queue here, run it yourself'.

    Failure to enqueue also returns False rather than raising: an upload that
    can't reach Cloud Tasks should still be processed, in-thread, with the old
    weaker guarantee — degraded is better than refusing the upload.
    """
    if not enabled():
        return False
    try:
        from google.cloud import tasks_v2

        client = tasks_v2.CloudTasksClient()
        request = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{BASE_URL}{path}",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
            },
            # Retries are the whole point, but not forever: a packet that is
            # genuinely unreadable would otherwise be redelivered indefinitely.
            # The worker also distinguishes "this file is broken" (2xx, don't
            # retry) from "this instance failed" (5xx, do).
            "dispatch_deadline": {"seconds": 1800},
        }
        if INVOKER:
            request["http_request"]["oidc_token"] = {
                "service_account_email": INVOKER,
                "audience": BASE_URL,
            }
        client.create_task(parent=QUEUE, task=request)
        return True
    except Exception:                     # noqa: BLE001 - fall back, don't fail
        import logging
        logging.getLogger(__name__).exception(
            "Could not enqueue a task; running it in-process instead")
        return False


def verify_oidc(auth_header: Optional[str]) -> bool:
    """Is this callback really from our Cloud Tasks queue?

    The worker endpoint runs ingest and writes to the database, so it must not
    be callable by anyone who finds the URL. Cloud Tasks signs each request with
    an OIDC token for the invoker service account; this checks the signature and
    the audience.

    Returns False when a token is required and doesn't verify. When no invoker
    is configured (local, or a queue set up without OIDC) there's nothing to
    check and the route's own guard applies instead.
    """
    if not INVOKER:
        return True
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return False
    token = auth_header.split(None, 1)[1]
    try:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            token, ga_requests.Request(), audience=BASE_URL or None)
        return claims.get("email") == INVOKER and claims.get("email_verified", False)
    except Exception:
        return False
