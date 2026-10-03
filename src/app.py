"""Flask web UI for the admissions summary pipeline.

    python -m src.app          # then open http://127.0.0.1:5000

Upload a CSV/Excel -> the deterministic pipeline runs -> browse a descriptive
summary per applicant. No scoring or ranking; the app summarizes the data and
leaves the judgement to the reviewer. No pipeline logic lives here; this only
orchestrates src.run and renders the results.

This app renders full applicant PII (names, emails, test scores, SOP/LOR
text) with no login by default, so it ships with guardrails on by default
and opt-in escape hatches via environment variables:

  GRADAPP_SECRET_KEY    Session/flash cookie signing key. Auto-generated
                        (random, not persisted) if unset; set it explicitly
                        if flashed messages need to survive a process restart.
  GRADAPP_DEBUG         "1" enables Flask/Werkzeug debug mode (interactive
                        debugger + full tracebacks in the browser). Off by
                        default -- the debugger allows arbitrary code
                        execution if this process is ever reachable by
                        anyone else.
  GRADAPP_HOST          Bind address. Defaults to 127.0.0.1 (localhost only).
                        A non-loopback host requires GRADAPP_ALLOW_REMOTE=1
                        *and* GRADAPP_PASSWORD -- the app refuses to start
                        otherwise.
  GRADAPP_PORT          Defaults to 5000.
  GRADAPP_PASSWORD      If set, gates every route behind HTTP Basic Auth.
  GRADAPP_MAX_UPLOAD_MB Max request body size in MB. Defaults to 200.
  GRADAPP_FORCE_SECURE_COOKIES  "1" marks the session cookie Secure (only
                        send it over HTTPS). Enable this if you put a TLS
                        proxy in front of GRADAPP_ALLOW_REMOTE -- leave off
                        for plain-HTTP localhost use, or the cookie (and
                        with it CSRF protection) silently stops working.

Also on by default, not configurable: security response headers (CSP,
X-Frame-Options, etc.), per-form CSRF tokens, upload rate limiting, a
file-signature check on top of the extension allow-list, and an access log
at data/audit.log (method/path/status only -- no applicant data).
"""
from __future__ import annotations

import functools
import io
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Dict, List

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, send_file, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename

from . import jobs
from .run import run, reindex, summaries_markdown
from .merge import likely_duplicate_groups, summary_view, sources_present
from .documents import DOC_TYPES, infer_cas_id, ingest_document
from .ingest_zip import ingest_packet_folder, ingest_zip
from .review import degree_coverage, write_review_sheet
from .triage import (PROF_DECISIONS, SWIPES, ProfessorError, add_professor,
                     apply_professor_decision, apply_swipe, assign, assigned_to,
                     condense_card, deck, decided, load_professors,
                     professor_by_id, professor_deck, queues, remove_professor,
                     save_professors, shortlisted, update_professor)
from .status_report import write_status_report
from .prescreen_report import FILES as PRESCREEN_FILES, counts as prescreen_counts, \
    write_all as write_prescreen, zip_bytes
from .screening import (ADMIT, REVIEW, SEPARATE_FILES, TRACK_LABELS, courses_source,
                        evaluate as prescreen_evaluate, profile as prescreen_profile)
from . import taskq
from .storage import blobs_for, use_for_config
from .store import Store, db_path_for
from .users import (ADVISOR, PENDING, PROFESSOR, UserError, add_user,
                    approve_user, authenticate, linked_professors, load_users,
                    login_required_globally, pending_users, remove_user,
                    request_account, save_users)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Scratch space for uploads and generated files. Overridable because a
# container's application directory isn't a sensible place to write — on Cloud
# Run this points at /tmp, and storage.py mirrors anything that matters to the
# bucket, so losing it on restart costs nothing.
DATA = os.environ.get("GRADAPP_DATA", "").strip() or os.path.join(ROOT, "data")
FIELD_MAP = os.path.join(ROOT, "config", "csv_field_map.yaml")

# Extensions accepted per upload route, checked before a file ever touches
# disk. Mirrors what parse.py / documents.extract_text() actually support --
# anything else is refused up front instead of being saved and left to fail
# deep inside the pipeline (or just sit on disk as an unrecognized blob).
CSV_EXTS = {".csv", ".xlsx", ".xls", ".xlsm"}
ZIP_EXTS = {".zip"}
# The packets drop zone: a ZIP of packets, loose packet PDFs, or a mix.
PACKET_EXTS = {".zip", ".pdf"}
DOC_EXTS = {".pdf", ".docx", ".txt", ".md"}

# File-signature ("magic bytes") checks layered on top of the extension
# allow-list above -- a renamed executable or script still won't pass. Text
# formats (.csv/.txt/.md) have no reliable magic number, so those are instead
# checked for a shebang/PE/ELF header, which a legitimate text file will
# never start with.
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")   # .zip, .docx, .xlsx
_OLE_MAGIC = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)            # legacy .xls
_BINARY_RED_FLAGS = (b"MZ", b"\x7fELF", b"#!")                # PE / ELF / shebang


def _sniff_ok(filename: str, head: bytes) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    if any(head.startswith(flag) for flag in _BINARY_RED_FLAGS):
        return False
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if ext in (".zip", ".docx", ".xlsx", ".xlsm"):
        return any(head.startswith(m) for m in _ZIP_MAGIC)
    if ext == ".xls":
        return head.startswith(_OLE_MAGIC[0])
    return True   # .csv/.txt/.md: no magic number, red-flag check above is it


# Most filesystems cap a single name at 255 bytes. secure_filename() sanitizes
# but never shortens, so a long name reached open() and raised OSError 36 —
# a 500 on an upload that was otherwise fine.
_MAX_NAME = 120


def _safe_name(filename: str, default: str) -> str:
    name = secure_filename(filename or "") or default
    stem, ext = os.path.splitext(name)
    return stem[:_MAX_NAME] + ext[:16] if len(name) > _MAX_NAME else name


def _blobs():
    """File storage for the current data directory — local disk, mirrored to a
    bucket when GRADAPP_BUCKET is set. Resolved per call rather than once at
    import, because DATA is monkeypatched per test."""
    return blobs_for(DATA)


def _save_upload(file, dest: str) -> bool:
    """Sniff the file's signature before writing it to disk. Returns False
    (and does not save) if the content doesn't match what the extension
    claims to be."""
    head = file.stream.read(8)
    file.stream.seek(0)
    if not _sniff_ok(file.filename, head):
        return False
    _blobs().save_upload(file, dest)
    return True


def _serve(path: str, download_name: str):
    """Send a generated file, fetching it from the bucket first if this
    instance isn't the one that made it. Without this, downloading the review
    sheet works only if your request happens to land on the same container that
    built it — which it does in testing and doesn't in production."""
    if not _blobs().ensure(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=download_name)


app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))

# Which world are we in? Everything below keys off this. "local" keeps the
# laptop-friendly defaults that make this pleasant to develop against; anything
# else means the app is reachable by people who aren't you, and the defaults
# inverto to fail-closed. Set GRADAPP_ENV=cloud in the container.
DEPLOYMENT = os.environ.get("GRADAPP_ENV", "local").strip().lower()
IS_CLOUD = DEPLOYMENT != "local"

# Guardrail against a half-applied configuration. If cloud infrastructure is
# wired up but GRADAPP_ENV never arrived, the app would come up in local mode --
# no forced HTTPS cookies, a 4-character password floor, and happy to serve
# applicant data with no login at all -- and nothing about it would look wrong.
# That is not hypothetical: repeating --set-env-vars in a gcloud deploy silently
# keeps only the last one, which drops GRADAPP_ENV and leaves the rest.
if not IS_CLOUD:
    _CLOUD_SIGNS = [name for name in
                    ("GRADAPP_DATABASE_URL", "GRADAPP_BUCKET", "GRADAPP_TASK_QUEUE")
                    if os.environ.get(name, "").strip()]
    if _CLOUD_SIGNS:
        raise SystemExit(
            f"{', '.join(_CLOUD_SIGNS)} is set but GRADAPP_ENV is "
            f"{DEPLOYMENT!r}. That combination means the cloud guardrails are "
            "off on what looks like a real deployment. Set GRADAPP_ENV=cloud, "
            "or unset those variables to run locally.")

# Guardrail: never ship a hardcoded secret key -- it's visible to anyone who
# reads this file and would let them forge the signed session/flash cookie.
#
# The random fallback is right on a laptop (a restart just logs you out) and
# wrong everywhere else: with more than one instance each gets a *different*
# key, so a session minted by one is rejected by the next -- users get logged
# out at random and it looks like a flaky app rather than a config error.
_SECRET = os.environ.get("GRADAPP_SECRET_KEY")
if IS_CLOUD and not _SECRET:
    raise SystemExit(
        "GRADAPP_ENV is not 'local' but GRADAPP_SECRET_KEY is unset. Generate "
        "one (`python -c 'import secrets;print(secrets.token_hex(32))'`), keep "
        "it in Secret Manager, and give every instance the same value.")
app.secret_key = _SECRET or secrets.token_hex(32)

# Guardrail: cap request size so one oversized upload can't exhaust memory or
# disk before any of the pipeline's own per-file size checks get a chance to
# run (ingest_zip has its own limits, but they only apply to a ZIP's *inside*).
app.config["MAX_CONTENT_LENGTH"] = (
    int(os.environ.get("GRADAPP_MAX_UPLOAD_MB", "200")) * 1024 * 1024
)

DEBUG = os.environ.get("GRADAPP_DEBUG", "0") == "1"
if IS_CLOUD and DEBUG:
    raise SystemExit(
        "GRADAPP_DEBUG=1 with GRADAPP_ENV=cloud. The Werkzeug debugger runs "
        "arbitrary code from the browser -- never on a reachable host.")
app.config["DEBUG"] = DEBUG

# Guardrail: session cookie hardening. HTTPONLY is Flask's default already;
# set it explicitly for clarity. SAMESITE=Lax means the cookie (and so the
# CSRF token tied to it) is never sent on a cross-site POST. SECURE is opt-in
# via env var because this app is normally served over plain HTTP on
# localhost -- forcing Secure there would silently break the cookie.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    IS_CLOUD or os.environ.get("GRADAPP_FORCE_SECURE_COOKIES") == "1")

# Guardrail: optional access control. Unset by default (fine for a single
# reviewer on localhost); set GRADAPP_PASSWORD to require HTTP Basic Auth on
# every route the moment more than one person can reach this machine.
_PASSWORD = os.environ.get("GRADAPP_PASSWORD")

# Guardrail: rate limiting on the state-changing routes, keyed by client IP.
# Generous enough for one reviewer clicking around, tight enough to blunt a
# scripted upload/auth-guessing loop. In-memory storage is fine for a
# single-process local tool.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://",
                  default_limits=[])

# Guardrail: append-only local access log (method/path/status/remote_addr
# only -- never applicant data) so a reviewer running this for a team can see
# who touched what, when. Configured lazily per-request against the current
# DATA dir rather than bound at import time, so it still respects DATA being
# pointed elsewhere (e.g. in tests).
_audit_logger = logging.getLogger("gradapp.audit")
_audit_logger.setLevel(logging.INFO)


_AUDIT_MAX_BYTES = 5_000_000   # rotate at ~5 MB; one .1 backup is kept


def _audit_log(line: str) -> None:
    try:
        os.makedirs(DATA, exist_ok=True)
        path = os.path.join(DATA, "audit.log")
        try:
            if os.path.getsize(path) > _AUDIT_MAX_BYTES:
                os.replace(path, path + ".1")   # rotate: keep one previous file
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # never let logging failures break a request


@app.before_request
def _require_auth():
    if not _PASSWORD:
        return None
    auth = request.authorization
    # auth.password is None for non-Basic schemes (e.g. Bearer) — treat as failed
    # auth rather than crashing compare_digest with a None.
    if (not auth or not auth.password
            or not secrets.compare_digest(auth.password, _PASSWORD)):
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="grad-app"'})
    return None


@app.before_request
def _same_origin_check():
    """Lightweight first line of CSRF defense: reject state-changing requests
    whose Origin/Referer doesn't match this host, before the (heavier)
    per-form token check below even runs."""
    if request.method != "POST":
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return None  # non-browser clients (CLI, tests) send neither; allow
    netloc = urlparse(origin).netloc
    if netloc and netloc != request.host:
        abort(403)
    return None


def _get_csrf_token() -> str:
    """Session-fixed CSRF token, generated on first use and reused for the
    life of the browser session. Exposed to templates as csrf_token()."""
    tok = session.get("_csrf_token")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf_token"] = tok
    return tok


app.jinja_env.globals["csrf_token"] = _get_csrf_token


@app.before_request
def _csrf_protect():
    """Validate the per-form token against the one tied to this session.
    Skipped in test mode (app.testing) so the test suite can drive routes
    directly without simulating a full GET-then-POST browser flow; the
    same-origin check above still applies even then."""
    if request.method != "POST" or app.testing:
        return None
    if request.endpoint == "task_run":
        # Cloud Tasks has no session and no form; its OIDC token is the
        # authentication, checked in the route itself.
        return None
    sent = request.form.get("csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, description="Missing or invalid CSRF token — reload the page and try again.")
    return None


@app.after_request
def _security_headers(response: Response) -> Response:
    """Defense-in-depth response headers. The CSP allows 'unsafe-inline' for
    script/style because the templates use small inline <script>/style
    blocks rather than a build step -- it still blocks loading any *remote*
    script/style/frame, which is the more likely injection vector here."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    return response


@app.after_request
def _access_log(response: Response) -> Response:
    _audit_log(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
              f"{request.remote_addr} {request.method} {request.path} -> {response.status_code}")
    return response


# --------------------------------------------------------------- accounts --
#
# Login is off until `config/users.yaml` has someone in it (see src/users.py).
# With no accounts this is the single-reviewer tool it has always been; with
# accounts, every page needs a session and the role decides what you can reach.

# Endpoints reachable without a session, so the login page itself can render.
_OPEN_ENDPOINTS = {"login", "logout", "setup", "signup", "static",
                   "task_run"}

# Four is fine on a laptop -- a demo needs a password someone can type off a
# slide, and the real protection there is that the app refuses to bind beyond
# loopback. On anything reachable that reasoning evaporates, so the floor goes
# up with the environment rather than relying on someone remembering to raise
# it.
_MIN_PASSWORD = 12 if IS_CLOUD else 4

# Guardrail: the app runs with *no login at all* until an account exists. That
# is the right default on a laptop and catastrophic on a public URL -- an empty
# users.yaml in a fresh container would serve every applicant's file to anyone
# who found the address.
#
# So in cloud mode the first account has to be claimed with a token only the
# operator has. Refuse to boot without one, rather than starting wide open and
# hoping whoever finds /setup first is you.
_SETUP_TOKEN = os.environ.get("GRADAPP_SETUP_TOKEN", "")
if IS_CLOUD and not _SETUP_TOKEN and not login_required_globally():
    raise SystemExit(
        "No accounts exist and GRADAPP_SETUP_TOKEN is unset, so the app would "
        "serve applicant data with no login. Set GRADAPP_SETUP_TOKEN to a "
        "random string, open /setup?token=<it> once to create the advisor "
        "account, then remove the variable.")


if IS_CLOUD and taskq.QUEUE and not taskq.INVOKER:
    raise SystemExit(
        "GRADAPP_TASK_QUEUE is set but GRADAPP_TASK_INVOKER is not, so "
        "/tasks/run would accept anything that finds it. Set the invoker "
        "service account Cloud Tasks signs callbacks with.")


def _setup_token_ok() -> bool:
    """Cloud only: /setup needs the operator's token before it will mint the
    first advisor. Locally it stays open -- there's nobody else on the box."""
    if not IS_CLOUD:
        return True
    offered = request.values.get("token", "")
    return bool(_SETUP_TOKEN) and secrets.compare_digest(offered, _SETUP_TOKEN)


def current_user():
    return session.get("user") if login_required_globally() else None


def _is(role: str) -> bool:
    """No accounts configured -> one operator who can do everything."""
    user = current_user()
    return user["role"] == role if user else True


@app.context_processor
def _inject_user():
    return {"user": current_user(),
            "logins_on": login_required_globally(),
            "is_advisor": _is(ADVISOR), "is_professor": _is(PROFESSOR)}


@app.before_request
def _require_login():
    if request.endpoint in _OPEN_ENDPOINTS:
        return None
    if not login_required_globally():
        # No accounts. On a laptop that's single-reviewer mode and everything
        # stays open. On a reachable host it means the app hasn't been claimed
        # yet, and open would mean open to the internet.
        if IS_CLOUD:
            return redirect(url_for("setup"))
        return None
    if not session.get("user"):
        if _wants_json():
            return {"error": "Sign in first."}, 401
        return redirect(url_for("login", next=request.path))
    return None


def role_required(role: str):
    """Gate a route to one role. A professor poking at an advisor URL gets a
    403, not a redirect — silently bouncing them to their own page would hide
    the fact that the link they were sent is not for them."""
    def decorate(view):
        @functools.wraps(view)
        def wrapped(*a, **kw):
            if not _is(role):
                abort(403, description=f"That page is for the {role}.")
            return view(*a, **kw)
        return wrapped
    return decorate


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("40/hour", methods=["POST"])
def login():
    if not login_required_globally():
        return redirect(url_for("index"))
    if request.method == "POST":
        user = authenticate(request.form.get("username", ""),
                            request.form.get("password", ""))
        if not user:
            flash("That username and password don't match.")
            return redirect(url_for("login"))
        if user["role"] == PENDING:
            # Correct password, no role yet. Safe to be specific here precisely
            # because they've already proved who they are — the generic message
            # above is what everyone else gets.
            flash("Your account is still waiting for an advisor to approve it.")
            return redirect(url_for("login"))
        session["user"] = {"username": user["username"], "role": user["role"],
                           "professor": user["professor"]}
        nxt = request.args.get("next") or request.form.get("next") or ""
        # Only ever bounce to our own paths — an absolute URL here would make
        # the login form an open redirect.
        target = nxt if nxt.startswith("/") and not nxt.startswith("//") else None
        return redirect(target or url_for("index"))
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
@limiter.limit("10/hour", methods=["POST"])
def signup():
    """Ask for an account. Creates a request, not access.

    Only offered once real accounts exist — before that /setup is the way in,
    and having two "create an account" doors on a wide-open app is how you end
    up with the wrong one being used.
    """
    if not login_required_globally():
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if password != request.form.get("confirm", ""):
            flash("Those passwords don't match.")
            return redirect(url_for("signup"))
        if len(password) < _MIN_PASSWORD:
            flash(f"Use at least {_MIN_PASSWORD} characters.")
            return redirect(url_for("signup"))
        try:
            save_users(request_account(load_users(), username, password))
        except UserError:
            # Deliberately vague, and deliberately the same words as success:
            # "that name is taken" on an open form tells a stranger exactly who
            # has an account here.
            pass
        flash("Request sent. An advisor has to approve it before you can sign in.")
        return redirect(url_for("login"))
    return render_template("signup.html", min_password=_MIN_PASSWORD)


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
@limiter.limit("20/hour", methods=["POST"])
def setup():
    """Create the first advisor account, from the browser.

    Only available while there are no accounts at all — otherwise this page
    would be a way for anyone who can reach the app to mint themselves an
    advisor login. After the first account exists, use Accounts (or the CLI).
    """
    if login_required_globally():
        flash("Accounts already exist — sign in, then use Accounts to add more.")
        return redirect(url_for("login"))
    if not _setup_token_ok():
        # 404 rather than 403: an unclaimed instance shouldn't advertise that
        # there's a first-advisor page here waiting to be taken.
        abort(404)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if password != request.form.get("confirm", ""):
            flash("Those passwords don't match.")
            return redirect(url_for("setup"))
        if len(password) < _MIN_PASSWORD:
            flash(f"Use at least {_MIN_PASSWORD} characters.")
            return redirect(url_for("setup"))
        try:
            save_users(add_user([], username, password, ADVISOR))
        except UserError as exc:
            flash(str(exc))
            return redirect(url_for("setup"))
        session["user"] = {"username": username, "role": ADVISOR, "professor": ""}
        flash(f"Signed in as {username}. Your professors sign up themselves — "
              "approve them under Accounts.")
        return redirect(url_for("accounts"))
    return render_template("setup.html")


@app.route("/accounts")
@role_required(ADVISOR)
def accounts():
    """Who can sign in. Advisor-only: it hands out access."""
    people = load_users()
    faculty = load_professors()
    taken = linked_professors(people)
    # Only offer professors who can actually be assigned. Listing the taken
    # ones greyed out reads as "pick me and something reasonable happens"; what
    # actually happens is a second person on that professor's queue.
    return render_template("accounts.html",
                           users=[u for u in people if u["role"] != PENDING],
                           waiting=pending_users(people),
                           faculty=faculty,
                           available=[p for p in faculty if p["id"] not in taken],
                           taken=taken)


@app.route("/accounts/approve", methods=["POST"])
@role_required(ADVISOR)
@limiter.limit("60/hour")
def accounts_approve():
    username = request.form.get("username", "")
    professor = request.form.get("professor", "")
    new_name = request.form.get("new_professor", "").strip()
    note = ""

    # Approving someone who isn't on the faculty list yet is the common case
    # for a new hire, and making the advisor leave the page, add them, and come
    # back was friction with nothing behind it. Create the entry here and link
    # it in one step. It's the same person doing the same two things.
    if professor == "__new__" or (new_name and not professor):
        try:
            faculty = add_professor(load_professors(), new_name)
        except ProfessorError as exc:
            flash(str(exc))
            return redirect(url_for("accounts"))
        save_professors(faculty)
        professor = faculty[-1]["id"]
        note = f" Added {new_name} to the faculty list."

    try:
        save_users(approve_user(load_users(), username,
                                request.form.get("role", ""), professor))
        flash(f"Approved {username}." + note)
    except UserError as exc:
        flash(str(exc))
    return redirect(url_for("accounts"))


#
# There is deliberately no route here for an advisor to create someone else's
# login. Everyone picks their own password: the first advisor at /setup, and
# everyone after that at /signup, where the advisor's job is to approve the
# request and say what the person is. An advisor typing a colleague's password
# means that password exists in a third place -- their head, the handover
# message, and the hash -- and it is the one path that can't be made safe by
# hashing. `python -m src.users add` remains for genuine out-of-band cases.


@app.route("/accounts/remove", methods=["POST"])
@role_required(ADVISOR)
@limiter.limit("60/hour")
def accounts_remove():
    username = request.form.get("username", "")
    me = (current_user() or {}).get("username")
    if username == me:
        # Removing the last advisor would drop the app back to no-login mode
        # for everyone; removing yourself is the usual way to do it by accident.
        flash("You can't remove the account you're signed in with.")
        return redirect(url_for("accounts"))
    try:
        save_users(remove_user(load_users(), username))
        flash(f"Removed {username}.")
    except UserError as exc:
        flash(str(exc))
    return redirect(url_for("accounts"))


def _ext_ok(filename: str, allowed: set) -> bool:
    return os.path.splitext(filename)[1].lower() in allowed


def _store() -> Store:
    return Store(db_path_for(DATA))


# Share job progress through the store, so a poll that lands on a different
# instance than the one doing the ingest can still answer. On a laptop this
# resolves to the same SQLite file and costs a row per job.
jobs.use_store(_store)

# users.yaml and professors.yaml are written while the app runs, so in a
# container they need the same bucket treatment as uploads — otherwise the
# advisor account created at /setup is gone on the next deploy.
use_for_config(blobs_for(DATA))


def _summaries() -> List[Dict[str, Any]]:
    return _store().index()


def _student_record(cas_id: str) -> Dict[str, Any]:
    return _store().get(cas_id) or {}


@app.route("/")
def index():
    """Where a login lands. A professor only ever has their own queue, so send
    them straight to it rather than to a dashboard about other people's work."""
    user = current_user()
    if user and user["role"] == PROFESSOR:
        return redirect(url_for("professor_home"))
    entries = _summaries()
    professors = load_professors()
    board, unassigned = queues(entries, professors)
    for prof in board:
        prof["assigned"] = assigned_to(entries, prof["id"])
        prof["waiting"] = [e for e in prof["assigned"] if not e.get("professor_status")]
        prof["accepted"] = [e for e in prof["assigned"]
                            if e.get("professor_status") == "accept"]
        prof["rejected"] = [e for e in prof["assigned"]
                            if e.get("professor_status") == "reject"]
    return render_template(
        "dashboard.html", professors=board, unassigned=unassigned,
        total=len(entries), tbd=len(deck(entries)),
        reviewed=len(decided(entries)),
        shortlist=len(shortlisted(entries)),
        answered=sum(1 for e in entries if e.get("professor_status")))


@app.route("/step/1")
@role_required(ADVISOR)
def step_upload():
    """Step 1 — upload only. Deliberately shows no summaries: a card before the
    status pass is reading the reviewer hasn't asked for yet."""
    store = _store()
    entries = store.index()
    return render_template("step_upload.html", count=len(entries),
                           coverage=degree_coverage(entries) if entries else None,
                           quarantine=len(store.quarantine()))


# ------------------------------------------------------------- professors --

def _my_professor_id() -> str:
    """Which professor the session is. With no accounts configured there is one
    operator, so fall back to the id in the URL query for a quick local look."""
    user = current_user()
    if user and user["role"] == PROFESSOR:
        return user["professor"]
    return request.args.get("as") or ""


@app.route("/prof")
def professor_home():
    """A professor's own deck: the students the advisor handed them."""
    prof_id = _my_professor_id()
    prof = professor_by_id(prof_id)
    if not prof:
        # No account and no ?as= — offer the list rather than a blank page.
        return render_template("prof_pick.html", professors=load_professors())
    entries = _summaries()
    mine = assigned_to(entries, prof_id)
    waiting = professor_deck(entries, prof_id)
    cards = [dict(e, card_text=condense_card(e.get("summary_text", "")))
             for e in waiting[:DECK_BATCH]]
    shown, page, pages = _page_of(mine, request.args.get("page", 1, type=int))
    return render_template("prof_deck.html", professor=prof, cards=cards,
                           mine=shown, queue_total=len(mine), page=page, pages=pages,
                           remaining=len(waiting) - len(cards),
                           accepted=[e for e in mine if e.get("professor_status") == "accept"],
                           rejected=[e for e in mine if e.get("professor_status") == "reject"])


@app.route("/prof/decide", methods=["POST"])
@limiter.limit("600/hour")
def professor_decide():
    prof_id = _my_professor_id()
    cas_id = request.form.get("cas_id", "")
    decision = request.form.get("decision", "")
    if decision not in PROF_DECISIONS:
        abort(400, description="Unknown decision.")
    entry = next((e for e in _summaries() if e["cas_id"] == cas_id), None)
    if not entry:
        abort(404)
    # A professor may only rule on their own queue — the id comes from the
    # session, so a copied link to someone else's student changes nothing.
    if entry.get("assigned_to") != prof_id:
        abort(403, description="That applicant isn't in your queue.")
    updated = _store().update(cas_id,
                              lambda u: apply_professor_decision(u, decision),
                              create=False)
    return {"cas_id": cas_id, "professor_status": updated.get("professor_status", "")}


@app.route("/step/2")
@role_required(ADVISOR)
def step_sheet():
    """Step 2 — download the roster with the degree column and a blank status."""
    store = _store()
    entries = store.index()
    return render_template("step_sheet.html", count=len(entries),
                           coverage=degree_coverage(entries) if entries else None)


@app.route("/step/3")
@role_required(ADVISOR)
def step_status():
    """Step 3 — take the marked-up spreadsheet back."""
    entries = _summaries()
    return render_template("step_status.html",
                           marked=sum(1 for e in entries if e.get("review_status")),
                           tbd=len(deck(entries)))


@app.route("/step/3", methods=["POST"], endpoint="step_status_post")
@limiter.limit("30/hour")
@role_required(ADVISOR)
def step_status_upload():
    """The marked-up sheet (and optionally more packets) coming back in."""
    sheet = request.files.get("csv")
    if not sheet or not sheet.filename:
        flash("Choose the spreadsheet with the status column filled in.")
        return redirect(url_for("step_status"))
    if not _ext_ok(sheet.filename, CSV_EXTS):
        flash(f"Unsupported file type — use {', '.join(sorted(CSV_EXTS))}.")
        return redirect(url_for("step_status"))

    dest = os.path.join(DATA, "raw", _safe_name(sheet.filename, "reviewed.xlsx"))
    if not _save_upload(sheet, dest):
        flash("That file's contents don't match its extension — refused.")
        return redirect(url_for("step_status"))

    packets = [f for f in request.files.getlist("zip") if f and f.filename]
    bad = [f.filename for f in packets if not _ext_ok(f.filename, PACKET_EXTS)]
    staged, refused = ([], []) if bad else _stage_packets(packets)
    back_url, deck_url = url_for("step_status"), url_for("triage")

    def work(progress):
        msgs = []
        progress.stage("Reading the spreadsheet")
        try:
            s = run(dest, DATA, FIELD_MAP)
            msgs.append(f"Read {s['rows']} row(s).")
        except Exception as exc:
            app.logger.exception("Error processing the marked-up spreadsheet")
            return {"message": f"Error reading the spreadsheet: {exc}",
                    "next": back_url}
        if bad:
            msgs.append(f"Packets must be .zip or .pdf — skipped {', '.join(bad[:3])}.")
        if refused:
            msgs.append(f"Refused (contents don't match the extension): {', '.join(refused)}.")
        if staged:
            msgs.append(_ingest_staged_packets(staged, progress))

        entries = _summaries()
        marked = sum(1 for e in entries if e.get("review_status"))
        tbd = len(deck(entries))
        if not marked:
            msgs.append("No status values were recognized — check the 'status' column "
                        "says accept, reject or tbd.")
        else:
            msgs.append(f"{marked} marked, {tbd} tbd to read.")
        return {"message": " ".join(msgs), "next": deck_url if tbd else back_url}

    if _wants_json():
        return {"job": jobs.start(work, "Starting")}

    result = work(jobs.Progress("sync"))
    flash(result["message"])
    return redirect(result["next"])


@app.route("/download/status.xlsx")
@role_required(ADVISOR)
def download_status():
    """Every applicant, every verdict, one row each — for circulating."""
    entries = _summaries()
    if not entries:
        flash("No applicants yet — upload a file first.")
        return redirect(url_for("index"))
    path = os.path.join(DATA, "reports", "applicant_status.xlsx")
    write_status_report(entries, load_professors(), path)
    _blobs().publish(path)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _serve(path, f"applicant_status_{stamp}.xlsx")


# One page of applicant cards. Each card carries a full summary, so rendering
# every one of five thousand produced a 19 MB page — fast on the server, hostile
# in a browser.
PAGE_SIZE = 50

# Cards sent to a deck at a time; the browser fetches more as it runs down.
DECK_BATCH = 25


def _page_of(items, page: int, size: int = PAGE_SIZE):
    """(slice, page, pages) with the page clamped into range."""
    pages = max(1, (len(items) + size - 1) // size)
    page = min(max(page, 1), pages)
    start = (page - 1) * size
    return items[start:start + size], page, pages


@app.route("/applicants")
@role_required(ADVISOR)
def applicants():
    """Everyone on file, decided or not — the browse view, off the main flow."""
    store = _store()
    entries = store.index()
    quarantine = store.quarantine()
    flagged = sum(1 for s in entries if s.get("warnings"))
    # How much of the degree column the packets could actually fill — an empty
    # cell means "no packet" or "packet names no degree", never a silent failure.
    coverage = degree_coverage(entries) if entries else None
    # Same person under two ids reads as two applicants unless it's called out.
    dupes = likely_duplicate_groups(entries)
    dupe_ids = {m["cas_id"]: [x["cas_id"] for x in g if x["cas_id"] != m["cas_id"]]
                for g in dupes for m in g}
    shown, page, pages = _page_of(entries, request.args.get("page", 1, type=int))
    return render_template("applicants.html", summaries=shown, coverage=coverage,
                           quarantine=quarantine, flagged=flagged, dupes=dupes,
                           dupe_ids=dupe_ids, tbd_count=len(deck(entries)),
                           total=len(entries), page=page, pages=pages)


@app.route("/upload", methods=["POST"])
@limiter.limit("30/hour")
@role_required(ADVISOR)
def upload():
    file = request.files.get("csv")
    if not file or not file.filename:
        flash("Please choose a CSV or Excel file.")
        return redirect(url_for("index"))
    if not _ext_ok(file.filename, CSV_EXTS):
        flash(f"Unsupported file type — use {', '.join(sorted(CSV_EXTS))}.")
        return redirect(url_for("index"))
    fname = _safe_name(file.filename, "upload.csv")
    dest = os.path.join(DATA, "raw", fname)
    if not _save_upload(file, dest):
        flash("That file's contents don't match its extension — refused.")
        return redirect(url_for("index"))
    try:
        summary = run(dest, DATA, FIELD_MAP)
        flash(f"Processed {summary['rows']} rows — "
              f"{summary['valid']} summarized, {summary['quarantined']} quarantined.")
    except Exception as exc:  # surface parsing errors to the user
        app.logger.exception("Error processing uploaded file %s", fname)
        flash(f"Error processing file: {exc}")
    return redirect(url_for("index"))


def _clear_processed(purge_raw: bool = False) -> int:
    """Clear all processed students + quarantine from the store and delete the
    generated report. By default raw uploads under data/raw/ are left alone.
    Pass purge_raw=True to also delete them -- that's the only way to remove
    an applicant's original file bytes (CSV/PDF/DOCX, still full of PII) from
    disk; merely clearing the browsable records does not do that. Returns
    students cleared."""
    store = _store()
    n = store.clear_students()
    store.clear_quarantine()
    _blobs().remove(os.path.join(DATA, "reports", "_summaries.md"))
    # Legacy pre-SQLite stores (data/students/*.json, data/quarantine/*.json)
    # hold the same derived PII as the database — clear them too, or "Clear
    # all" silently leaves full applicant records on disk.
    for legacy in (os.path.join(DATA, "students"), os.path.join(DATA, "quarantine")):
        if os.path.isdir(legacy):
            for fn in os.listdir(legacy):
                if fn.endswith(".json"):
                    try:
                        os.remove(os.path.join(legacy, fn))
                    except OSError:
                        pass
    if purge_raw:
        raw = os.path.join(DATA, "raw")
        # Bucket first: these are the applicants' original files, and leaving
        # them in Cloud Storage while telling the advisor they were deleted is
        # the worst possible outcome of this button.
        _blobs().remove_tree(raw)
        for root, _dirs, files in os.walk(raw):
            for fn in files:
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass
    return n


@app.route("/reset", methods=["POST"])
@limiter.limit("30/hour")
@role_required(ADVISOR)
def reset():
    """Clear all processed students (start fresh). Raw uploads are kept
    unless the 'purge_raw' checkbox was submitted -- check it to actually
    delete applicant files from disk, not just the derived records."""
    purge = bool(request.form.get("purge_raw"))
    n = _clear_processed(purge_raw=purge)
    msg = f"Cleared all processed data ({n} student record(s))."
    if purge:
        msg += " Raw uploaded files were also deleted."
    msg += " Upload to start fresh."
    if _wants_json():
        return {"message": msg, "cleared": n}
    flash(msg)
    return redirect(url_for("index"))


@app.route("/process", methods=["GET", "POST"])
@limiter.limit("30/hour", methods=["POST"])
@role_required(ADVISOR)
def process():
    """Handle the CSV/Excel and/or the ZIP in one submit. CSV is processed first
    (creates the application sections), then the ZIP merges resume/SOP into the
    same records by cas_id."""
    if request.method == "GET":
        return redirect(url_for("index"))
    csv_file = request.files.get("csv")
    # Packets arrive as a ZIP, as loose PDFs, or as several of either — a
    # reviewer with a folder of packets shouldn't have to zip it first.
    packet_files = [f for f in request.files.getlist("zip") if f and f.filename]
    has_csv = bool(csv_file and csv_file.filename)
    has_packets = bool(packet_files)
    if not has_csv and not has_packets:
        flash("Choose a CSV/Excel file, packets (ZIP or PDFs), or both.")
        return redirect(url_for("index"))

    if has_csv and not _ext_ok(csv_file.filename, CSV_EXTS):
        flash(f"Application data file type not supported — use {', '.join(sorted(CSV_EXTS))}.")
        return redirect(url_for("index"))
    bad = [f.filename for f in packet_files if not _ext_ok(f.filename, PACKET_EXTS)]
    if bad:
        flash(f"Packets must be .zip or .pdf — {', '.join(bad[:3])} isn't.")
        return redirect(url_for("index"))

    # Read the uploads to disk here, on the request thread: a Werkzeug file
    # stream doesn't outlive the request, so a worker thread would find it
    # closed. Only the slow part (parsing, OCR) moves to the background.
    if request.form.get("replace"):
        _clear_processed()
    csv_path = None
    saved_packets, refused = [], []
    if has_csv:
        csv_path = os.path.join(DATA, "raw", _safe_name(csv_file.filename, "upload.csv"))
        if not _save_upload(csv_file, csv_path):
            csv_path = None
            refused.append("the application data file")
    if has_packets:
        saved_packets, bad_sniff = _stage_packets(packet_files)
        refused += bad_sniff

    # Resolve URLs here: the worker runs off the request thread, where Flask's
    # url_for has no application context to look them up in.
    next_url = url_for("step_sheet")

    def work(progress):
        msgs = []
        if refused:
            msgs.append(f"Refused (contents don't match the extension): {', '.join(refused)}.")
        if csv_path:
            progress.stage("Reading the spreadsheet")
            try:
                s = run(csv_path, DATA, FIELD_MAP)
                msgs.append(f"Application data: {s['valid']} summarized, "
                            f"{s['quarantined']} quarantined.")
            except Exception as exc:
                app.logger.exception("Error processing application data in /process")
                msgs.append(f"Application data error: {exc}")
        if saved_packets:
            msgs.append(_ingest_staged_packets(saved_packets, progress))
        message = " ".join(msgs)
        return {"message": message, "next": next_url}

    if _wants_json():
        queued = _queue_ingest(csv_path, saved_packets, refused, next_url)
        if queued:
            return {"job": queued}
        return {"job": jobs.start(work, "Starting")}

    result = work(jobs.Progress("sync"))          # no JS: do it inline as before
    flash(result["message"])
    return redirect(url_for("step_sheet") if _store().count() else url_for("index"))


def _wants_json() -> bool:
    """True when the browser asked by fetch rather than by navigating.

    Every route below keeps its redirect-and-flash behaviour for a plain form
    post, so the app still works with JavaScript off — and so the test suite
    can drive it without simulating fetch.
    """
    return (request.headers.get("X-Requested-With") == "fetch"
            or request.accept_mimetypes.best == "application/json")


@app.route("/jobs/<job_id>")
def job_status(job_id: str):
    """Poll a background ingest: what it's reading now, and how far in."""
    job = jobs.get(job_id)
    if not job:
        abort(404)
    total, done_count = job.get("total") or 0, job.get("done_count") or 0
    return {
        "done": job["done"],
        "label": job.get("label") or "",
        "count": done_count,
        "total": total,
        "percent": round(100 * done_count / total) if total else (100 if job["done"] else 0),
        "error": job.get("error"),
        "message": (job.get("result") or {}).get("message") if isinstance(job.get("result"), dict) else None,
        "next": (job.get("result") or {}).get("next") if isinstance(job.get("result"), dict) else None,
    }


def _packet_report(r: Dict[str, Any]) -> str:
    m = (f"Packets: {r['pdfs_found']} PDF(s) → updated {r['students_updated']} student(s), "
         f"added {r['sections_added']} section(s), captured "
         f"{r['score_sections']} score/transcript section(s).")
    if r.get("scanned_docs"):
        m += f" OCR read {r['ocr_pages']} scanned page(s) across {r['scanned_docs']} document(s)."
    if r.get("ocr_unavailable_docs"):
        m += f" ⚠ {r['ocr_unavailable_docs']} document(s) had scanned pages but OCR isn't installed."
    if r["unmatched_files"]:
        m += f" {len(r['unmatched_files'])} unmatched."
    return m


def _stage_packets(files):
    """Write the uploaded packets to disk and return their paths.

    Saving happens on the request thread because an upload stream is closed the
    moment the request ends — a worker thread would find nothing to read. Each
    upload gets its own timestamped folder, so a later upload doesn't re-read
    (and re-OCR) every packet ever sent.
    """
    batch = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stage_dir = os.path.join(DATA, "raw", "packets", batch)
    saved, refused = [], []
    for f in files:
        name = _safe_name(f.filename, "packet.pdf")
        is_zip = os.path.splitext(name)[1].lower() == ".zip"
        dest = os.path.join(DATA, "raw", name) if is_zip else os.path.join(stage_dir, name)
        if _save_upload(f, dest):
            saved.append(dest)
        else:
            refused.append(f.filename)
    return saved, refused


def _ingest_staged_packets(paths, progress=None) -> str:
    """Merge already-saved packets — ZIPs and loose PDFs down the same path."""
    totals = {"pdfs_found": 0, "students_updated": 0, "sections_added": 0,
              "score_sections": 0, "unmatched_files": [], "scanned_docs": 0,
              "ocr_pages": 0, "ocr_unavailable_docs": 0}

    def _add(report):
        for k, v in report.items():
            if k in totals:
                totals[k] += v

    notes = []
    zips = [p for p in paths if p.lower().endswith(".zip")]
    loose = [p for p in paths if not p.lower().endswith(".zip")]
    for path in zips:
        try:
            _add(ingest_zip(path, DATA, progress))
        except Exception as exc:
            app.logger.exception("Error processing a packets ZIP")
            notes.append(f"{os.path.basename(path)}: {exc}")
    if loose:
        try:
            _add(ingest_packet_folder(os.path.dirname(loose[0]), DATA, progress))
        except Exception as exc:
            app.logger.exception("Error processing loose packet PDFs")
            notes.append(f"Loose PDFs: {exc}")
    return " ".join([_packet_report(totals)] + notes)


def _queue_ingest(csv_path, packet_paths, refused, next_url) -> str:
    """Hand each uploaded file to the queue. Returns a job id, or "" if there
    is no queue and the caller should run the work itself.

    One task per file rather than one per upload: a batch of forty scanned
    packets as a single task would sit near the dispatch deadline and lose all
    of it on a redelivery, whereas per file a retry repeats one packet.
    """
    if not taskq.enabled():
        return ""
    units = ([{"kind": "roster", "path": csv_path}] if csv_path else [])
    for path in packet_paths:
        units.append({"kind": "zip" if path.lower().endswith(".zip") else "packet",
                      "path": path})
    if not units:
        return ""

    notes = ([f"Refused (contents don't match the extension): {', '.join(refused)}."]
             if refused else [])
    job_id = jobs.open_fanout(len(units), "Queued", next_url, notes)

    sent = 0
    for unit in units:
        if taskq.enqueue({**unit, "job": job_id, "next": next_url}):
            sent += 1
    if sent < len(units):
        # Partially enqueued: better to run the whole thing in-thread than to
        # leave a job that can never reach its total.
        app.logger.error("Only %s of %s tasks enqueued; falling back", sent, len(units))
        return ""
    return job_id


def _ingest_one(unit: Dict[str, Any]) -> str:
    """Do one queued unit of work. Must be safe to run twice.

    Retries are the point of the queue, so this gets called again whenever a
    worker dies mid-file or returns 5xx. That's safe because records merge by
    cas_id and a source is replaced per originating file, so re-reading a packet
    converges on the same record instead of duplicating anything.
    """
    kind, path = unit.get("kind"), unit.get("path", "")
    # The file was uploaded to some other instance; fetch it before reading.
    if path and not _blobs().ensure(path):
        raise FileNotFoundError(f"{os.path.basename(path)} is no longer available")

    if kind == "roster":
        s = run(path, DATA, FIELD_MAP)
        return (f"Application data: {s['valid']} summarized, "
                f"{s['quarantined']} quarantined.")
    if kind == "zip":
        return _packet_report(ingest_zip(path, DATA))
    if kind == "packet":
        # ingest_packet_folder works on a directory; give it one holding just
        # this file so a retry re-reads one packet rather than the whole batch.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, os.path.basename(path))
            os.symlink(os.path.abspath(path), link)
            return _packet_report(ingest_packet_folder(tmp, DATA))
    raise ValueError(f"Unknown task kind {kind!r}")


@app.route("/tasks/run", methods=["POST"])
def task_run():
    """Cloud Tasks calls this. Never a browser.

    Status codes are the contract with the queue, so they're chosen carefully:
    a 5xx means "redeliver this", a 2xx means "done with it". A packet we can't
    parse must therefore return 2xx — it will fail identically forever, and
    redelivering it would occupy the queue until the deadline while the
    reviewer waits for a job that can never complete.
    """
    # Fails closed: with no queue configured this endpoint does nothing and
    # shouldn't exist. Otherwise an unconfigured deployment would expose an
    # unauthenticated route that writes to the database.
    if not taskq.enabled():
        abort(404)
    if not taskq.verify_oidc(request.headers.get("Authorization")):
        abort(403)
    unit = request.get_json(silent=True) or {}
    job_id = unit.get("job", "")
    try:
        message = _ingest_one(unit)
    except Exception as exc:                       # noqa: BLE001
        app.logger.exception("Queued ingest unit failed")
        name = os.path.basename(unit.get("path", "")) or "a file"
        # Ask for one redelivery on a transient-looking failure, then give up
        # and record it against the job so the reviewer sees which file.
        if int(request.headers.get("X-CloudTasks-TaskRetryCount", "0")) < 2:
            return {"error": str(exc)}, 503
        jobs.finish_unit(job_id, f"{name}: {exc}", unit.get("next", ""))
        return {"error": str(exc), "gave_up": True}, 200

    jobs.finish_unit(job_id, message, unit.get("next", ""))
    return {"ok": True, "message": message}, 200


@app.route("/upload_zip", methods=["POST"])
@limiter.limit("30/hour")
@role_required(ADVISOR)
def upload_zip():
    file = request.files.get("zip")
    if not file or not file.filename:
        flash("Please choose a ZIP file.")
        return redirect(url_for("index"))
    if not _ext_ok(file.filename, ZIP_EXTS):
        flash("The packets file must be a .zip.")
        return redirect(url_for("index"))
    dest = os.path.join(DATA, "raw", _safe_name(file.filename, "packets.zip"))
    if not _save_upload(file, dest):
        flash("That file's contents don't match its extension — refused.")
        return redirect(url_for("index"))
    try:
        r = ingest_zip(dest, DATA)
        msg = (f"Processed {r['pdfs_found']} PDF(s): updated {r['students_updated']} "
               f"student(s), added {r['sections_added']} section(s), "
               f"captured {r['score_sections']} score/transcript section(s).")
        if r["unmatched_files"]:
            msg += f" {len(r['unmatched_files'])} file(s) could not be matched."
        flash(msg)
    except Exception as exc:
        app.logger.exception("Error processing ZIP in /upload_zip")
        flash(f"Error processing ZIP: {exc}")
    return redirect(url_for("index"))


@app.route("/upload_document", methods=["POST"])
@limiter.limit("60/hour")
@role_required(ADVISOR)
def upload_document():
    file = request.files.get("doc")
    doc_type = (request.form.get("type") or "").strip()
    cas_id = (request.form.get("cas_id") or "").strip()
    recommender = (request.form.get("recommender") or "").strip() or None
    if not file or not file.filename:
        flash("Please choose a document.")
        return redirect(url_for("index"))
    if doc_type not in DOC_TYPES:
        flash(f"Type must be one of {', '.join(DOC_TYPES)}.")
        return redirect(url_for("index"))
    if not _ext_ok(file.filename, DOC_EXTS):
        flash(f"Unsupported file type — use {', '.join(sorted(DOC_EXTS))}.")
        return redirect(url_for("index"))
    fname = _safe_name(file.filename, "document")
    cas_id = cas_id or infer_cas_id(fname)
    if not cas_id:
        flash("Could not determine cas_id — enter it, or name the file '<cas_id>_sop.pdf'.")
        return redirect(url_for("index"))
    # Guard against typos creating phantom student records: this route only
    # attaches to an existing student. (The CLI, src.add_document, can still
    # create a record when documents arrive before the application data.)
    if not _store().get(cas_id):
        flash(f"No student '{cas_id}' on file — document not attached. Check the ID, "
              "or upload the application data first.")
        return redirect(url_for("index"))
    dest = os.path.join(DATA, "raw", "documents", fname)
    if not _save_upload(file, dest):
        flash("That file's contents don't match its extension — refused.")
        return redirect(url_for("index"))
    try:
        _, words = ingest_document(dest, cas_id, doc_type, DATA, recommender=recommender)
        reindex(DATA, changed={cas_id}, reconcile=False, write_report=False)
        flash(f"Added {doc_type} ({words} words) to {cas_id}.")
    except Exception as exc:
        app.logger.exception("Error reading document for %s in /upload_document", cas_id)
        flash(f"Error reading document: {exc}")
    return redirect(url_for("student", cas_id=cas_id))


@app.route("/review-sheet")
@role_required(ADVISOR)
def review_sheet():
    """Step 1 of the review pass: the roster with the undergrad degree filled in
    and a blank status column to mark up."""
    entries = _summaries()
    if not entries:
        flash("No applicants yet — upload a file first.")
        return redirect(url_for("index"))
    out = os.path.join(DATA, "reports", "applicants_review.xlsx")
    raw = os.path.join(DATA, "raw")
    try:
        # This reads the roster the advisor originally uploaded, to preserve
        # every one of their columns. On more than one instance that upload
        # probably landed somewhere else.
        _blobs().ensure_tree(raw)
        write_review_sheet(entries, raw, out, link_base=_public_base())
        _blobs().publish(out)
    except Exception as exc:
        app.logger.exception("Error building the review spreadsheet")
        flash(f"Could not build the review spreadsheet: {exc}")
        return redirect(url_for("index"))
    return _serve(out, "applicants_review.xlsx")


@app.route("/summaries")
@role_required(ADVISOR)
def summaries():
    """The reviewed set: what you shortlisted or rejected in the deck."""
    entries = _summaries()
    reviewed = decided(entries)
    shown, page, pages = _page_of(reviewed, request.args.get("page", 1, type=int))
    return render_template("summaries.html", summaries=shown, total=len(entries),
                           reviewed=len(reviewed), page=page, pages=pages,
                           kept=sum(1 for e in reviewed if e.get("triage") == "shortlist"),
                           waiting=len(deck(entries)))


@app.route("/download/summaries")
@role_required(ADVISOR)
def download_summaries():
    """Built fresh on the way out. A swipe changes what belongs in the report
    but doesn't re-run the pipeline, so the cached file on disk goes stale the
    moment you triage anything."""
    entries = _summaries()
    if not entries:
        flash("No applicants yet — upload a file first.")
        return redirect(url_for("index"))
    path = os.path.join(DATA, "reports", "_summaries.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(summaries_markdown(entries))
    _blobs().publish(path)
    return _serve(path, "applicant_summaries.md")


# ------------------------------------------------------------- prescreen --

def _public_base() -> str:
    """The address links in exported spreadsheets point at. The request's own
    address by default; GRADAPP_PUBLIC_URL when people reach the app by a
    different name than the one this request used (a proxy, a custom domain)."""
    return os.environ.get("GRADAPP_PUBLIC_URL", "").strip() or request.url_root


_TRACK_SORT = {"quick_admit": 0, "ms_distance": 1, "phd_distance": 2, "on_campus": 3,
               "other_distance": 4, "no_program": 5}
_OUTCOME_SORT = {ADMIT: 0, REVIEW: 1, "": 2}


@app.route("/prescreen")
@role_required(ADVISOR)
def prescreen():
    """The grad office's rules, applied as flags, plus the three spreadsheets."""
    entries = _summaries()
    track = request.args.get("track", "")
    outcome = request.args.get("outcome", "")
    shown = [e for e in entries
             if (not track or (e.get("screening") or {}).get("track") == track)
             and (not outcome or (e.get("screening") or {}).get("outcome") == outcome)]
    shown.sort(key=lambda e: (_TRACK_SORT.get((e.get("screening") or {}).get("track"), 9),
                              _OUTCOME_SORT.get((e.get("screening") or {}).get("outcome", ""), 9),
                              str(e.get("name", "")).lower()))
    rows, page, pages = _page_of(shown, request.args.get("page", 1, type=int))
    return render_template("prescreen.html", counts=prescreen_counts(entries),
                           labels=TRACK_LABELS, rows=rows, track=track, outcome=outcome,
                           page=page, pages=pages, total=len(entries), filtered=len(shown),
                           separate=SEPARATE_FILES, source=courses_source(),
                           ADMIT=ADMIT, REVIEW=REVIEW)


@app.route("/prescreen/reapply", methods=["POST"])
@limiter.limit("30/hour")
@role_required(ADVISOR)
def prescreen_reapply():
    """Re-run the rules (and re-render summaries) for everyone on file, after
    config/screening_rules.yaml or the course list changed."""
    n = _store().refresh_index()
    flash(f"Rules re-applied to {n} applicant(s).")
    return redirect(url_for("prescreen"))


def _prescreen_files() -> Dict[str, str]:
    outdir = os.path.join(DATA, "reports")
    paths = write_prescreen(_summaries(), _public_base(), outdir)
    for path in paths.values():
        _blobs().publish(path)
    return paths


@app.route("/download/prescreen/<kind>.xlsx")
@role_required(ADVISOR)
def download_prescreen(kind: str):
    if kind not in PRESCREEN_FILES:
        abort(404)
    if not _summaries():
        flash("No applicants yet — upload a file first.")
        return redirect(url_for("index"))
    path = _prescreen_files()[kind]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _serve(path, PRESCREEN_FILES[kind].replace(".xlsx", f"_{stamp}.xlsx"))


@app.route("/download/prescreen.zip")
@role_required(ADVISOR)
def download_prescreen_zip():
    if not _summaries():
        flash("No applicants yet — upload a file first.")
        return redirect(url_for("index"))
    data = zip_bytes(_prescreen_files())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return send_file(io.BytesIO(data), as_attachment=True, mimetype="application/zip",
                     download_name=f"prescreen_{stamp}.zip")


@app.route("/triage")
@role_required(ADVISOR)
def triage():
    """The swipe pass: one card per tbd applicant, right to shortlist, left to
    reject."""
    entries = _summaries()
    waiting = deck(entries)
    # Only a batch reaches the browser. Every tbd card in the DOM at once was
    # 9 MB of HTML at five thousand applicants, and you can only read the top
    # one — the rest arrive as you work through them.
    cards = [dict(e, card_text=condense_card(e.get("summary_text", "")))
             for e in waiting[:DECK_BATCH]]
    return render_template("triage.html", cards=cards, professors=load_professors(),
                           shortlist_count=len(shortlisted(entries)),
                           remaining=len(waiting) - len(cards),
                           total_tbd=sum(1 for e in entries
                                         if e.get("review_status") == "tbd"))


@app.route("/triage/cards")
@role_required(ADVISOR)
def triage_cards():
    """The next batch of deck cards, as HTML the page appends."""
    waiting = deck(_summaries())
    seen = set(request.args.getlist("seen"))
    nxt = [e for e in waiting if e["cas_id"] not in seen][:DECK_BATCH]
    cards = [dict(e, card_text=condense_card(e.get("summary_text", "")))
             for e in nxt]
    return {"html": render_template("_deck_cards.html", cards=cards,
                                    professors=load_professors()),
            "remaining": max(0, len(waiting) - len(seen) - len(cards))}


@app.route("/triage/swipe", methods=["POST"])
@limiter.limit("600/hour")
@role_required(ADVISOR)
def triage_swipe():
    """A swipe, optionally naming the professor to hand them to.

    Picking the professor on the card saves a second pass through the
    shortlist; leaving it blank is fine, they just land there unassigned.
    """
    cas_id = request.form.get("cas_id", "")
    direction = request.form.get("direction", "")
    prof_id = request.form.get("professor", "")
    if direction not in SWIPES:
        abort(400, description="Unknown swipe direction.")
    if prof_id and not professor_by_id(prof_id):
        abort(400, description="Unknown professor.")

    def _apply(unified):
        apply_swipe(unified, direction)
        if direction == "shortlist" and prof_id:
            assign(unified, prof_id)
        return unified

    updated = _store().update(cas_id, _apply, create=False)
    if updated is None:
        abort(404)
    return {"cas_id": cas_id, "triage": updated.get("triage", ""),
            "review_status": updated.get("review_status", ""),
            "assigned_to": updated.get("assigned_to", "")}


@app.route("/triage/shortlist")
@role_required(ADVISOR)
def triage_shortlist():
    """What survived the swipe, and which professor each one goes to."""
    professors = load_professors()
    entries = shortlisted(_summaries())
    for e in entries:
        e["professor"] = professor_by_id(e.get("assigned_to", ""), professors)
    return render_template("shortlist.html", applicants=entries,
                           professors=professors,
                           remaining=len(deck(_summaries())))


@app.route("/triage/assign", methods=["POST"])
@limiter.limit("600/hour")
@role_required(ADVISOR)
def triage_assign():
    cas_id = request.form.get("cas_id", "")
    prof_id = request.form.get("professor", "")
    professors = load_professors()
    if prof_id and not professor_by_id(prof_id, professors):
        abort(400, description="Unknown professor.")
    updated = _store().update(cas_id, lambda u: assign(u, prof_id), create=False)
    if updated is None:
        abort(404)
    who = updated.get("identity", {}).get("full_name", cas_id)
    to = (professor_by_id(prof_id, professors) or {}).get("name", "nobody")
    message = f"{who} → {to}"
    if _wants_json():
        return {"message": message, "cas_id": cas_id, "professor": prof_id, "name": to}
    flash(message)
    return redirect(url_for("triage_shortlist"))


@app.route("/professors")
@role_required(ADVISOR)
def professors_view():
    """Each professor's review queue, and the list itself."""
    profs, unassigned = queues(_summaries(), load_professors())
    return render_template("professors.html", professors=profs, unassigned=unassigned,
                           editing=request.args.get("edit") == "1")


def _save_professors(professors, message: str):
    """Persist an edited faculty list and report back."""
    try:
        save_professors(professors)
    except OSError as exc:
        app.logger.exception("Could not write the professor list")
        message = f"Could not save the professor list: {exc}"
        if _wants_json():
            return {"message": message, "error": True}, 500
    if _wants_json():
        return {"message": message}
    flash(message)
    return redirect(url_for("professors_view", edit=1))


def _professor_error(exc):
    if _wants_json():
        return {"message": str(exc), "error": True}, 400
    flash(str(exc))
    return redirect(url_for("professors_view", edit=1))


@app.route("/professors/add", methods=["POST"])
@limiter.limit("120/hour")
@role_required(ADVISOR)
def professors_add():
    name = request.form.get("name", "")
    try:
        professors = add_professor(load_professors(), name,
                                   request.form.get("url", ""),
                                   request.form.get("areas", ""))
    except ProfessorError as exc:
        return _professor_error(exc)
    return _save_professors(professors, f"Added {name.strip()}.")


@app.route("/professors/update", methods=["POST"])
@limiter.limit("120/hour")
@role_required(ADVISOR)
def professors_update():
    prof_id = request.form.get("id", "")
    try:
        professors = update_professor(load_professors(), prof_id,
                                      name=request.form.get("name", ""),
                                      url=request.form.get("url", ""),
                                      areas=request.form.get("areas", ""))
    except ProfessorError as exc:
        return _professor_error(exc)
    return _save_professors(professors, "Saved.")


@app.route("/professors/delete", methods=["POST"])
@limiter.limit("120/hour")
@role_required(ADVISOR)
def professors_delete():
    """Remove a professor, and unassign anyone left in their queue so no record
    points at an id that no longer exists."""
    prof_id = request.form.get("id", "")
    professors = load_professors()
    gone = professor_by_id(prof_id, professors)
    try:
        remaining = remove_professor(professors, prof_id)
    except ProfessorError as exc:
        return _professor_error(exc)

    store = _store()
    freed = [e["cas_id"] for e in store.index() if e.get("assigned_to") == prof_id]
    for cas_id in freed:
        store.update(cas_id, lambda u: assign(u, ""), create=False)

    note = f"Removed {(gone or {}).get('name', prof_id)}."
    if freed:
        note += (f" {len(freed)} applicant(s) went back to the shortlist, "
                 "unassigned.")
    return _save_professors(remaining, note)


@app.route("/student/<cas_id>")
def student(cas_id: str):
    unified = _student_record(cas_id)
    if not unified:
        abort(404)
    rec = summary_view(unified)                 # flattened view for the template
    summary = {"summary_text": unified.get("summary", "")}
    return render_template("student.html", rec=rec, summary=summary,
                           unified=unified, sources_present=sources_present(unified),
                           screening=prescreen_evaluate(unified),
                           profile=prescreen_profile(unified), ADMIT=ADMIT)


if __name__ == "__main__":
    host = os.environ.get("GRADAPP_HOST", "127.0.0.1")
    port = int(os.environ.get("GRADAPP_PORT", "5000"))
    allow_remote = os.environ.get("GRADAPP_ALLOW_REMOTE") == "1"

    # Guardrail: refuse to bind beyond localhost unless explicitly opted in,
    # and never without auth -- this app renders applicant PII with no login
    # by default.
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise SystemExit(
            f"Refusing to bind to a non-loopback address ({host!r}) without "
            "GRADAPP_ALLOW_REMOTE=1. This app renders applicant PII (names, "
            "emails, test scores, SOP/LOR text) with no auth by default.")
    if allow_remote and not _PASSWORD:
        raise SystemExit(
            "GRADAPP_ALLOW_REMOTE=1 requires GRADAPP_PASSWORD to also be "
            "set -- refusing to serve applicant PII on a non-loopback "
            "address with no authentication.")
    if DEBUG:
        print("⚠ GRADAPP_DEBUG=1: the interactive Werkzeug debugger is "
              "enabled. Never set this if the process is reachable by "
              "anyone you would not hand a shell to.", flush=True)

    # use_reloader=False is important: uploads are saved under data/, and the
    # auto-reloader would otherwise restart the server mid-upload and kill the
    # request. Keep the friendly debugger pages only when DEBUG is explicitly
    # requested, but never restart-on-file-write.
    app.run(host=host, port=port, debug=DEBUG, use_reloader=False)
