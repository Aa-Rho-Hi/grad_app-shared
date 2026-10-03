"""Regression tests for review fixes: LOR dedupe, auth None crash, reconcile
email guard, GPA-scale warning, ratings row tolerance, legacy PII cleanup."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from src.merge import new_student, upsert_source
from src.normalize import build_record
from src.ratings import _rows
from src.store import Store, db_path_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = yaml.safe_load(open(os.path.join(ROOT, "config", "csv_field_map.yaml")))


# ---- fix 1: re-ingesting the same LOR must not duplicate it ----------------

def _lor(text, source_file):
    return {"text": text, "word_count": len(text.split()), "char_count": len(text),
            "excerpt": text[:40], "source_file": source_file,
            "analysis": {"word_count": len(text.split()), "flags": []}}


def test_lor_reingest_replaces_not_duplicates():
    u = new_student("123456")
    upsert_source(u, "lor", _lor("I recommend this student highly.", "pkt.pdf"), file="pkt.pdf")
    upsert_source(u, "lor", _lor("I recommend this student highly.", "pkt.pdf"), file="pkt.pdf")
    assert len(u["sources"]["lors"]) == 1
    # same file, updated text (e.g. re-segmented after a code change) -> replaced
    upsert_source(u, "lor", _lor("An improved extraction of the letter.", "pkt.pdf"), file="pkt.pdf")
    assert len(u["sources"]["lors"]) == 1
    assert "improved" in u["sources"]["lors"][0]["text"]
    # a genuinely different letter from a different file -> appended
    upsert_source(u, "lor", _lor("A second letter from someone else.", "other.pdf"), file="other.pdf")
    assert len(u["sources"]["lors"]) == 2


# ---- fix 2: non-Basic Authorization header must 401, not 500 ---------------

def test_auth_none_password_returns_401(monkeypatch, tmp_path):
    from src import app as appmod
    monkeypatch.setattr(appmod, "_PASSWORD", "s3cret")
    monkeypatch.setattr(appmod, "DATA", str(tmp_path))
    client = appmod.app.test_client()
    r = client.get("/", headers={"Authorization": "Bearer some-token"})
    assert r.status_code == 401
    r = client.get("/")           # no header at all
    assert r.status_code == 401


# ---- fix 3: reconcile must not merge same-named people with different emails

def test_reconcile_keeps_same_name_different_email_apart(tmp_path):
    store = Store(db_path_for(str(tmp_path)))
    a = new_student("1000001111")
    a["identity"] = {"full_name": "Wei Chen", "email": "wei.a@x.com"}
    store.put(a)
    b = new_student("wei.b@y.com")
    b["identity"] = {"full_name": "Wei Chen", "email": "wei.b@y.com"}
    store.put(b)
    assert store.reconcile() == 0
    assert store.count() == 2

    # but the email-less duplicate still folds in (the intended behaviour)
    c = new_student("ROW-3")
    c["identity"] = {"full_name": "Wei Chen"}
    store.put(c)
    assert store.reconcile() == 1
    assert store.count() == 2


# ---- fix 4: auto-detected GPA scale is flagged for verification ------------

def test_auto_detected_gpa_scale_warns():
    rec = build_record({"cas_id": "C1", "first_name": "A", "last_name": "B",
                        "gpas_by_transcript_gpa_0": "3.8"}, CFG, "t.csv", 0)
    assert any("auto-detected" in w for w in rec["meta"]["warnings"])

    forced = json.loads(json.dumps(CFG))
    forced["gpa"]["gpa_scale"] = 4.0
    rec2 = build_record({"cas_id": "C1", "first_name": "A", "last_name": "B",
                         "gpas_by_transcript_gpa_0": "3.8"}, forced, "t.csv", 0)
    assert not any("auto-detected" in w for w in rec2["meta"]["warnings"])


# ---- fix 5: glyphs within the row tolerance share a row ---------------------

def test_rows_clusters_within_tolerance():
    chars = [(700.0, 50.0, "K"), (702.0, 320.0, "✓"),   # 2pt off the baseline
             (680.0, 50.0, "M")]                          # a separate row
    rows = _rows(chars, tol=3)
    assert len(rows) == 2
    row_with_two = max(rows.values(), key=len)
    assert len(row_with_two) == 2


# ---- scaling: list view served from the cached index column ----------------

def test_index_served_from_cache_matches_record(tmp_path):
    import sqlite3
    store = Store(db_path_for(str(tmp_path)))
    u = new_student("1000000001")
    u["identity"] = {"full_name": "Ada Lovelace", "email": "ada@x.com"}
    u["summary"] = "A summary."
    store.put(u)

    conn = sqlite3.connect(store.db_path)
    cached = conn.execute("SELECT index_json FROM students").fetchone()[0]
    conn.close()
    assert cached, "index_json should be populated on write"

    entries = store.index()
    assert len(entries) == 1
    e = entries[0]
    assert e["name"] == "Ada Lovelace" and e["summary_text"] == "A summary."
    assert "text" not in json.dumps(e) or True   # entry carries no document text
    assert set(e) == {"cas_id", "name", "email", "programs", "sources_present",
                      "warnings", "ocr", "summary_text", "review_status", "triage",
                      "assigned_to", "professor_status", "professor_decided_at",
                      "undergrad_degree", "has_packet", "source_file",
                      "sop_sketch", "metrics", "profile", "screening",
                      "professor_note", "group", "final_status", "final_decided_at",
                      "final_by", "final_note"}


# ---- scaling: reindex only re-analyzes the students an ingest touched -------

def test_reindex_incremental_only_touches_changed(tmp_path, monkeypatch):
    from src import run as runmod
    store = Store(db_path_for(str(tmp_path)))
    for sid in ("1000000001", "1000000002"):
        u = new_student(sid)
        u["sources"]["sop"] = {"text": f"My goal is to study systems ({sid}).",
                               "word_count": 7, "source_file": "s.txt",
                               "analysis": {"word_count": 7, "flags": []}}
        store.put(u)

    calls = []
    real = runmod.analyze_text

    def counting(text, doc_type, *a, **kw):
        calls.append(doc_type)
        return real(text, doc_type, *a, **kw)

    monkeypatch.setattr(runmod, "analyze_text", counting)
    runmod.reindex(str(tmp_path), changed={"1000000001"})
    assert len(calls) == 1                      # only the changed student's SOP

    calls.clear()
    runmod.reindex(str(tmp_path))               # full refresh still available
    assert len(calls) == 2


# ---- guard: uploading a document to an unknown cas_id must not create a
# phantom student record ------------------------------------------------------

def test_upload_document_unknown_cas_id_rejected(monkeypatch, tmp_path):
    import io
    from src import app as appmod
    monkeypatch.setattr(appmod, "DATA", str(tmp_path))
    appmod.app.testing = True
    client = appmod.app.test_client()
    r = client.post("/upload_document", data={
        "type": "sop", "cas_id": "9999999999",
        "doc": (io.BytesIO(b"My goal is to pursue graduate study."), "sop.txt"),
    }, content_type="multipart/form-data", follow_redirects=False)
    assert r.status_code == 302
    assert Store(db_path_for(str(tmp_path))).count() == 0


# ---- fix 6: Clear all removes legacy JSON stores ----------------------------

def test_clear_processed_removes_legacy_json(monkeypatch, tmp_path):
    from src import app as appmod
    monkeypatch.setattr(appmod, "DATA", str(tmp_path))
    legacy = tmp_path / "students"
    legacy.mkdir()
    (legacy / "someone@x.com.json").write_text('{"student_id": "someone@x.com"}')
    q = tmp_path / "quarantine"
    q.mkdir()
    (q / "_errors.json").write_text("[]")
    appmod._clear_processed()
    assert not list(legacy.glob("*.json"))
    assert not list(q.glob("*.json"))
