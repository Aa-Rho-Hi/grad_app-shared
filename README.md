# Grad App — Applicant Summary Pipeline (CSV/Excel → JSON → Summary)

Deterministic pipeline that turns an application CSV or Excel file into a strict,
validated canonical JSON record, then produces a **factual descriptive summary**
for each applicant. **No LLM, no scoring, no ranking** — the app summarizes the
data and leaves the judgement to the reviewer.

> **New here?** [RUNBOOK.md](RUNBOOK.md) is the full operational guide: clone & setup,
> architecture so far, technologies used, how to run and open everything, and a
> file-by-file reference for the entire repo.

## Clone & run

```bash
git clone <repo-url> grad-app && cd grad-app
python -m venv .venv && source .venv/bin/activate            # optional but recommended
pip install -r requirements.txt -c constraints.txt          # pinned, reproducible
python -m src.run --input tests/fixtures/sample_applications.csv   # CLI (demo data)
python -m src.app                                            # web UI at http://127.0.0.1:5000
pytest -q                                                    # tests
# records persist in data/students.db (SQLite); the report lands in data/reports/_summaries.md
```

## Development & CI

```bash
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
python -m pyflakes src                                       # lint
pytest --cov=src --cov-report=term-missing --cov-fail-under=80
```

`constraints.txt` pins the tested dependency versions; `requirements-dev.txt`
adds the test/lint tooling. GitHub Actions (`.github/workflows/ci.yml`) runs the
lint + coverage-gated suite on Python 3.10–3.12 (installing Tesseract so the
scanned-PDF OCR test runs) on every push and pull request.

Point `--input` at your real **CSV or Excel** file (`.csv`, `.xlsx`, `.xls`, `.xlsm`).
Headers must match the mapping below (extra columns are ignored). Excel is read with
pandas/openpyxl, and numeric-looking IDs (e.g. `1001.0`) are cleaned back to text.

## What it does

1. **parse.py** — reads the file with **pandas** (robust to quoting, newlines, BOM, encodings, and Excel) and collapses indexed columns (`name_0`, `name_1`, …) into arrays.
2. **normalize.py** — assembles the record, maps columns via candidate name-lists, normalizes GPA to a 4.0 scale, and records data-quality warnings.
3. **models.py / validate.py** — the strong parser: a typed **Pydantic v2** model coerces types, enforces ranges, forbids unknown fields, and emits precise per-field errors. Bad rows go to `data/quarantine/`. The model is the single source of truth for `student.schema.json` — regenerate with `python -m scripts.gen_schema`.
4. **template.py** — renders a neutral, factual **summary** per applicant (no scoring).
5. **app.py** — Flask UI: upload a file, browse summaries, download all summaries, see data-to-verify flags and quarantined rows.

## Column mapping (real export)

| Source columns | Canonical field |
|---|---|
| `cas_id` → else `email` → else generated | `cas_id` |
| `first_name`, `last_name`, `email` | `personal.*` |
| `ielts_official_overall_band_score` / `ielts_overall_band_score_*` | `english_proficiency.ielts[]` + `best_ielts_overall` |
| `toefl_ibt_official_score_*` / `toefl_ibt_*_*` | `english_proficiency.toefl[]` + `best_toefl_total` |
| `gre_general_official_overall_result_*` | `gre_results[]` (reported as-is) |
| `transcript_college_*` / `college_name_*` + `gpas_by_transcript_gpa_*` | `education[]` (with `gpa`) |
| `designation_*` (+ `local_status_*`, department/level/term/year) | `programs[]` (program applied to + status) |
| `custom_questions_*_what_area_of_specialization` / `*_which_area_*` | `interests.*` |

**Protected attributes** (race, gender, age, citizenship, phone) are deliberately **not** parsed.

## Key behaviours

- **Identifier**: `cas_id` if present, else `email`, else a generated `ROW-n`.
- **GPA scale varies per university**, so it is auto-detected per value (≤4.3→4.0, ≤5→5.0, ≤10→10.0, ≤20→20.0, else→100) and normalized to 4.0. Force a scale with `gpa.gpa_scale` in `config/csv_field_map.yaml`.
- **Data-quality flags** (e.g. "No GRE score on file") are factual notes for the reviewer, shown in the UI — not judgements.

## Prescreen (grad office rules)

The ECEN grad office's admit rules, applied as **flags, never decisions**:
each applicant comes out "Admit" or "Further review" with the reasons spelled
out, plus a "check by hand" list (letterhead, signatures, the advisor being
TAMU faculty) that a person confirms even on an Admit.

| Track (from the CAS designation) | Admit when |
|---|---|
| Quick Admit, on campus | ECE department and GPA 3.25+ (everyone else: further review) |
| MS Distance, non-thesis | ECE, GPA 3.25+, 10 courses from the approved distance list (max two 400-level, rest 600/700) |
| MS Distance, thesis | all of the above, plus a faculty advisor confirmation and the employer letter (Option A) or unemployment statement (Option B) in the exact required wording |
| PhD Distance | 6 approved courses with an MS (14 with a Bachelor's only) and the PhD employer letter in the exact wording |

- **Config, not code.** Thresholds, the ECE major list and the letter wording
  are in `config/screening_rules.yaml`; the approved course list (copied from
  the department's distance-learning page) is `config/distance_courses.yaml`.
  After editing either, press **Re-apply rules** on the Prescreen page.
- **Spreadsheets.** `/prescreen` downloads three workbooks: a summary for Quick
  Admit and the other on-campus programs, and separate MS Distance and PhD
  Distance files. Each has the fields the office asked for (schools + GPA,
  degree type, major, GRE, English proficiency, awards, domestic/international),
  a "Summaries" tab with the full written summary, a "Rules" tab, and a
  clickable link on every row to the applicant's page. The review sheet
  (`/review-sheet`) gets the same link column. Set `GRADAPP_PUBLIC_URL` if
  people reach the app at a different address than the one you download from.
- **Domestic / International** is the one field derived from citizenship: only
  that two-word label is stored (read off the "... - International Students"
  designation first, the export's citizenship status second). The country is
  never read, and no rule uses the label.
- Letters and course plans are read from the packet pages kept at ingest
  (`packet_evidence` on the record), so rules re-run without re-uploading.

## Documents (resumes / SOPs / LORs)

Supporting documents extend only the extraction layer — the schema, summary step,
and the no-LLM boundary stay the same. They can be ingested three ways:

```bash
python -m src.add_document --input "1001_sop.pdf" --type sop     # single file
python -m src.ingest_zip   --input packets.zip                   # ZIP of combined "Full Application" PDFs
# or via the web UI: attach a single doc, or upload a CSV + ZIP together
```

Combined packets are split into resume/SOP/LOR sections automatically (score
cards dropped), scanned pages are read with OCR (Tesseract, optional), and each
section merges into the student's record by `cas_id`. See `ARCHITECTURE.md` and
`architecture.svg` for the full picture, and `RUNBOOK.md` for per-file docs and
troubleshooting.

## Security & privacy guardrails (web UI)

`src.app` renders full applicant PII (names, emails, test scores, SOP/LOR
text) and has **no login by default** — it's built for a single reviewer on
their own machine. It ships with these guardrails on by default, and
opt-in escape hatches via environment variables:

| Env var | Default | Purpose |
|---|---|---|
| `GRADAPP_SECRET_KEY` | random per-process | Session/flash cookie signing key. Never hardcode this — set it explicitly only if flashed messages need to survive a restart. |
| `GRADAPP_DEBUG` | off | `1` enables the interactive Werkzeug debugger. Leave off — it allows arbitrary code execution if the process is ever reachable by anyone else. |
| `GRADAPP_HOST` | `127.0.0.1` | Bind address. Binding elsewhere requires `GRADAPP_ALLOW_REMOTE=1` **and** `GRADAPP_PASSWORD` — the app refuses to start otherwise. |
| `GRADAPP_PASSWORD` | unset | If set, gates every route behind HTTP Basic Auth. |
| `GRADAPP_MAX_UPLOAD_MB` | `200` | Caps request body size, so one oversized upload can't exhaust memory/disk. |
| `GRADAPP_FORCE_SECURE_COOKIES` | off | `1` marks the session cookie `Secure` (HTTPS-only). Only enable this behind a TLS proxy — on plain HTTP it silently breaks the cookie (and CSRF protection with it). |

Other guardrails baked in, not configurable:

- **Extension allow-lists** on every upload route (`.csv/.xlsx/.xls/.xlsm`,
  `.zip`, `.pdf/.docx/.txt/.md`) — a mismatched file is rejected before it
  touches disk.
- **File-signature (magic-byte) checks** on top of the extension allow-list —
  a file renamed to `.pdf`/`.zip`/`.docx`/`.xlsx` but not actually shaped like
  one (or that looks like an executable/script) is refused even though its
  extension passed.
- **Per-form CSRF tokens**, session-bound and validated on every POST, plus a
  same-origin (`Origin`/`Referer`) check as a first line of defense before
  the token check even runs.
- **Rate limiting** (`Flask-Limiter`, in-memory) on every upload/reset route —
  generous for one reviewer clicking around, tight enough to blunt a scripted
  abuse loop.
- **Security response headers** — CSP, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, a locked
  down `Permissions-Policy` — plus `SameSite=Lax`/`HttpOnly` on the session
  cookie.
- **Access log.** Every request's method/path/status/remote address (never
  applicant data) is appended to `data/audit.log`.
- **Server-side error logging.** Pipeline failures are logged with a full
  traceback via `app.logger.exception`, in addition to the short message
  shown to the reviewer.
- **Raw-upload purge.** "Clear all" on the dashboard normally only clears the
  derived records; check "also delete raw uploads" to actually remove the
  original CSV/ZIP/document bytes (and the PII in them) from disk.
- **`.gitignore`** excludes `data/` (raw uploads + the SQLite store) so
  applicant PII can't be committed if this project is later put under git.
- **Protected attributes** (race, gender, age, citizenship, phone) are never
  parsed (see above) and every Pydantic model uses `extra="forbid"`, so even
  a future CSV export that adds one of those columns can't leak it into a
  record — enforced by `tests/test_app_guardrails.py`, not just documented.

`tests/test_app_guardrails.py` (24 tests) exercises all of the above:
extension/magic-byte rejection, the 413 size cap, the same-origin 403, real
CSRF-token enforcement, rate-limit 429s, optional Basic Auth, the audit log,
server-side error logging, purge behavior, and the protected-attribute/schema
check.
