"""Guardrail checks: privacy-by-construction (protected attributes never
enter the schema) and Flask app-security hardening (upload limits, extension
allow-lists, debug/secret-key defaults, same-origin check, optional auth,
raw-upload purge). These test the guardrails as behavior, not implementation,
so they keep protecting the app even if app.py is refactored later.
"""
import importlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "student.schema.json")

PROTECTED_TERMS = ("race", "ethnicity", "gender", "sex", "age", "citizenship",
                   "nationality", "phone", "disability", "religion")


# ---------------------------------------------------------------------------
# Privacy: protected attributes must never enter the schema or a record.
# ---------------------------------------------------------------------------

def test_protected_attributes_never_in_generated_schema():
    """student.schema.json is generated from the Pydantic models (the single
    source of truth for what a record can contain). None of its field names
    should reference a protected attribute -- this is what makes 'fairness by
    construction' an enforced guarantee rather than just a comment."""
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    field_names = set()

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    field_names.update(v.keys())
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    lowered = {f.lower() for f in field_names}
    for term in PROTECTED_TERMS:
        hits = [f for f in lowered if term in f]
        assert not hits, f"protected-attribute-like field(s) leaked into schema: {hits}"


def test_models_forbid_unknown_fields():
    """extra='forbid' on every model is what actually prevents a protected
    attribute from sneaking into a record even if a future CSV export adds a
    'gender' column -- normalize.py would have to explicitly map it (it
    doesn't), and even a raw pass-through would be rejected at validation."""
    from src import models
    import inspect
    checked = 0
    for name, obj in inspect.getmembers(models):
        if inspect.isclass(obj) and hasattr(obj, "model_config"):
            if getattr(obj, "__module__", "") == models.__name__:
                assert obj.model_config.get("extra") == "forbid", \
                    f"{name} does not forbid unknown/extra fields"
                checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# App security: reload src.app fresh per test so env-var-driven config
# (secret key, debug, password, upload cap) is exercised deterministically.
# ---------------------------------------------------------------------------

def _reload_app(monkeypatch, **env):
    for k in ("GRADAPP_SECRET_KEY", "GRADAPP_DEBUG", "GRADAPP_PASSWORD",
             "GRADAPP_MAX_UPLOAD_MB", "GRADAPP_HOST", "GRADAPP_ALLOW_REMOTE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "src.app" in sys.modules:
        mod = importlib.reload(sys.modules["src.app"])
    else:
        mod = importlib.import_module("src.app")
    mod.app.testing = True
    return mod


def test_debug_off_by_default(monkeypatch):
    mod = _reload_app(monkeypatch)
    assert mod.DEBUG is False
    assert mod.app.config["DEBUG"] is False


def test_debug_requires_explicit_opt_in(monkeypatch):
    mod = _reload_app(monkeypatch, GRADAPP_DEBUG="1")
    assert mod.DEBUG is True


def test_secret_key_is_not_hardcoded(monkeypatch):
    mod = _reload_app(monkeypatch)
    assert mod.app.secret_key not in (None, "", "grad-app-dev")
    assert len(mod.app.secret_key) >= 32


def test_secret_key_honors_env_override(monkeypatch):
    mod = _reload_app(monkeypatch, GRADAPP_SECRET_KEY="my-explicit-key")
    assert mod.app.secret_key == "my-explicit-key"


def test_bad_extension_rejected_for_csv_upload(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    data = {"csv": (io.BytesIO(b"not really a csv"), "malware.exe")}
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code in (302, 200)
    assert not os.path.exists(os.path.join(mod.DATA, "raw", "malware.exe"))


def test_bad_extension_rejected_for_document_upload(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    _seed_student(tmp_path)
    client = mod.app.test_client()
    data = {"doc": (io.BytesIO(b"#!/bin/sh\necho hi"), "1001_sop.sh"),
           "type": "sop", "cas_id": "CAS1001"}
    resp = client.post("/upload_document", data=data, content_type="multipart/form-data")
    assert resp.status_code in (302, 200)
    assert not os.path.exists(os.path.join(str(tmp_path), "raw", "documents", "1001_sop.sh"))


def test_bad_extension_rejected_for_zip_upload(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    data = {"zip": (io.BytesIO(b"PK\x03\x04fake"), "packets.tar")}
    resp = client.post("/upload_zip", data=data, content_type="multipart/form-data")
    assert resp.status_code in (302, 200)
    assert not os.path.exists(os.path.join(str(tmp_path), "raw", "packets.tar"))


def test_oversized_upload_rejected(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch, GRADAPP_MAX_UPLOAD_MB="1")
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    big = io.BytesIO(b"x" * (2 * 1024 * 1024))  # 2MB > 1MB cap
    data = {"csv": (big, "big.csv")}
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 413


def test_cross_origin_post_blocked(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    resp = client.post("/reset", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_same_origin_post_allowed(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    resp = client.post("/reset", headers={"Origin": "http://localhost"},
                       base_url="http://localhost")
    assert resp.status_code == 302


def test_basic_auth_required_when_password_set(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch, GRADAPP_PASSWORD="s3cret")
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    resp = client.get("/")
    assert resp.status_code == 401

    import base64
    creds = base64.b64encode(b"reviewer:wrong").decode()
    resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 401

    creds = base64.b64encode(b"reviewer:s3cret").decode()
    resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 200


def test_no_auth_required_when_password_unset(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200


def test_reset_purge_raw_deletes_uploaded_files(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    raw_dir = os.path.join(str(tmp_path), "raw", "documents")
    os.makedirs(raw_dir, exist_ok=True)
    leftover = os.path.join(raw_dir, "1001_sop.pdf")
    with open(leftover, "wb") as f:
        f.write(b"%PDF-1.4 fake")

    client = mod.app.test_client()
    resp = client.post("/reset", data={"purge_raw": "1"},
                       headers={"Origin": "http://localhost"},
                       base_url="http://localhost")
    assert resp.status_code == 302
    assert not os.path.exists(leftover)


def test_reset_without_purge_keeps_raw_files(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    raw_dir = os.path.join(str(tmp_path), "raw", "documents")
    os.makedirs(raw_dir, exist_ok=True)
    kept = os.path.join(raw_dir, "1001_sop.pdf")
    with open(kept, "wb") as f:
        f.write(b"%PDF-1.4 fake")

    client = mod.app.test_client()
    resp = client.post("/reset", headers={"Origin": "http://localhost"},
                       base_url="http://localhost")
    assert resp.status_code == 302
    assert os.path.exists(kept)


# ---------------------------------------------------------------------------
# Round 2: security headers, CSRF enforcement, rate limiting, magic bytes,
# audit logging, server-side error logging.
# ---------------------------------------------------------------------------

def test_security_headers_present(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_csrf_token_enforced_outside_test_mode(monkeypatch, tmp_path):
    """The CSRF check is skipped when app.testing is True (so the rest of
    this suite can drive routes directly); flip it off here to prove the
    check itself actually rejects a request with no/bad token."""
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    mod.app.testing = False
    client = mod.app.test_client()

    # No token at all -> rejected.
    resp = client.post("/reset", headers={"Origin": "http://localhost"},
                       base_url="http://localhost")
    assert resp.status_code == 400

    # Fetch a page first to mint a session + token, then use the *wrong* token.
    client.get("/step/1", base_url="http://localhost")   # a page with a form
    resp = client.post("/reset", data={"csrf_token": "not-the-real-token"},
                       headers={"Origin": "http://localhost"}, base_url="http://localhost")
    assert resp.status_code == 400


def test_csrf_token_accepted_when_valid(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    mod.app.testing = False
    client = mod.app.test_client()

    # Render a page in this session to get a real token issued, then read it
    # back out of the session via the test client's cookie jar.
    with client.session_transaction() as sess:
        pass  # establishes the session cookie
    client.get("/step/1", base_url="http://localhost")   # a page with a form
    with client.session_transaction() as sess:
        token = sess["_csrf_token"]

    resp = client.post("/reset", data={"csrf_token": token},
                       headers={"Origin": "http://localhost"}, base_url="http://localhost")
    assert resp.status_code == 302


def test_rate_limit_blocks_excessive_uploads(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    statuses = []
    for _ in range(35):  # /upload is capped at 30/hour
        data = {"csv": (io.BytesIO(b"bad ext, rejected before save"), "x.exe")}
        resp = client.post("/upload", data=data, content_type="multipart/form-data")
        statuses.append(resp.status_code)
    assert 429 in statuses



def _seed_student(tmp_path, cas_id="CAS1001"):
    """upload_document only attaches to an existing student (typo guard), so
    tests that exercise it must seed the record first."""
    from src.store import Store, db_path_for
    from src.merge import new_student
    Store(db_path_for(str(tmp_path))).put(new_student(cas_id))


def test_magic_bytes_reject_content_extension_mismatch(monkeypatch, tmp_path):
    """A file named *.pdf but containing something else (e.g. a renamed
    script) must be refused even though the extension allow-list passes."""
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    _seed_student(tmp_path)
    client = mod.app.test_client()
    data = {"doc": (io.BytesIO(b"#!/bin/sh\nrm -rf /"), "1001_sop.pdf"),
           "type": "sop", "cas_id": "CAS1001"}
    resp = client.post("/upload_document", data=data, content_type="multipart/form-data")
    assert resp.status_code in (302, 200)
    assert not os.path.exists(os.path.join(str(tmp_path), "raw", "documents", "1001_sop.pdf"))


def test_magic_bytes_accept_genuine_pdf(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    _seed_student(tmp_path)
    client = mod.app.test_client()
    data = {"doc": (io.BytesIO(b"%PDF-1.4\n%%EOF"), "1001_sop.pdf"),
           "type": "sop", "cas_id": "CAS1001"}
    resp = client.post("/upload_document", data=data, content_type="multipart/form-data")
    assert resp.status_code in (302, 200)
    # it was saved (even if text extraction later fails on this stub PDF)
    assert os.path.exists(os.path.join(str(tmp_path), "raw", "documents", "1001_sop.pdf"))


def test_audit_log_records_requests(monkeypatch, tmp_path):
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    client = mod.app.test_client()
    client.get("/")
    log_path = os.path.join(str(tmp_path), "audit.log")
    assert os.path.exists(log_path)
    with open(log_path) as f:
        contents = f.read()
    assert "GET / -> 200" in contents


def test_server_side_error_logged_on_pipeline_failure(monkeypatch, tmp_path, caplog):
    """Force the pipeline call to raise, and confirm the full exception is
    logged server-side (app.logger.exception) in addition to the short
    message flashed to the user."""
    mod = _reload_app(monkeypatch)
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    monkeypatch.setattr(mod, "run", lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom: bad row 3")))
    client = mod.app.test_client()
    with caplog.at_level("ERROR"):
        data = {"csv": (io.BytesIO(b"cas_id,first_name\n1,Alex\n"), "applicants.csv")}
        resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert any("Error processing uploaded file" in r.message for r in caplog.records)
    assert any("boom: bad row 3" in (r.exc_text or "") for r in caplog.records if r.exc_info)
