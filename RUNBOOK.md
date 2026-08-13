# Grad App — Runbook

Operational guide for the **Graduate Applicant Summary Pipeline**: what the project is, how to get it, how to run it, and what every file does. For design rationale see [ARCHITECTURE.md](ARCHITECTURE.md); for a quick overview see [README.md](README.md).

---

## 1. What this project is

A **fully local, deterministic pipeline** that turns graduate-admissions exports (CSV/Excel rows plus PDF application packets) into one validated, unified JSON record per applicant, then renders a **factual descriptive summary** for reviewers.

Hard constraints baked into the design:

- **No LLM, no scoring, no ranking** — the app summarizes data; the human reviewer judges.
- **No network** — everything runs on the reviewer's machine (SQLite, local OCR).
- **Protected attributes** (race, gender, age, citizenship, phone) are never parsed, and `extra="forbid"` on every model prevents them leaking in via new columns.
- **PII never committed** — `.gitignore` excludes all of `data/`.

## 2. Technologies used

| Layer | Technology |
|---|---|
| Language | Python 3.10–3.12 |
| Validation / schema | Pydantic v2 (single source of truth for `student.schema.json`) |
| Tabular ingestion | pandas + openpyxl (CSV, `.xlsx`, `.xls`, `.xlsm`) |
| PDF text extraction | pypdf; pypdfium2 for rendering & glyph positions |
| OCR (scanned pages, optional) | Tesseract via pytesseract + Pillow/numpy preprocessing; optional RapidOCR (`GRADAPP_OCR_ENGINE=rapidocr`) |
| DOCX extraction | python-docx |
| Storage | SQLite (WAL, atomic transactions) — `data/students.db` |
| Web UI | Flask + Flask-Limiter, Jinja2 templates |
| Config | YAML (`config/*.yaml`) |
| Tests / CI | pytest + pytest-cov (80% gate), pyflakes, reportlab (test fixtures), GitHub Actions matrix on 3.10/3.11/3.12 |

## 3. Architecture so far

Two ingestion paths converge on one unified per-student record keyed by `cas_id` (see `architecture.svg` / `architecture.png` / `architecture.mermaid`):

```
CSV/Excel ──▶ parse ──▶ normalize ──▶ validate (Pydantic) ──▶ application section ─┐
                                          │ (bad rows → data/quarantine/)          ├─▶ merge.py ──▶ unified record ──▶ store.py (SQLite) ──▶ template.py summary ──▶ CLI report / Flask UI
PDF/ZIP packets ─▶ segment (packet.py, Viterbi) ─▶ extract text (+OCR) ─▶ analyze ─┘
```

- **Application path**: `parse.py → normalize.py → models.py/validate.py`. Rows that fail validation are quarantined, not silently dropped.
- **Document path**: single files via `add_document.py`, or ZIPs of combined "Full Application" PDFs via `ingest_zip.py`. `packet.py` labels each page (resume/SOP/LOR/scores/ignore) with per-page feature scores decoded jointly by Viterbi; `ocr.py` handles scanned pages; `analyze.py` does keyword/structure analysis; `ratings.py` reads Likert reference-rating tables by glyph position.
- **Merge & storage**: `merge.py` upserts each source into a per-student record (schema v3.0.0, provenance tracked); `store.py` persists it atomically in SQLite (WAL, `BEGIN IMMEDIATE`, graceful fallback on network filesystems).
- **Output**: `template.py` renders the neutral summary; `run.py` writes `data/reports/_summaries.md`; `app.py` serves the same content as a web UI.

## 4. Clone & set up

```bash
git clone <repo-url> grad-app        # substitute your remote URL once one is configured
cd grad-app

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -c constraints.txt  # pinned, reproducible

# Optional — OCR for scanned PDFs:
brew install tesseract               # macOS
sudo apt-get install tesseract-ocr   # Ubuntu

# Optional — dev/test tooling:
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
```

`constraints.txt` pins the tested versions. OCR is optional: without Tesseract the pipeline still runs and flags scanned pages instead of crashing.

## 5. Run it

### CLI pipeline

```bash
python -m src.run --input tests/fixtures/sample_applications.csv   # demo data; accepts CSV or .xlsx/.xls/.xlsm
```

Records land in `data/students.db`; invalid rows in `data/quarantine/`; the combined report in `data/reports/_summaries.md`.

### Documents

```bash
python -m src.add_document --input "1001_sop.pdf" --type sop          # single SOP/LOR/resume
python -m src.ingest_zip   --input packets.zip                        # ZIP of combined application PDFs
```

`cas_id` is inferred from the filename prefix (`<cas_id>_...`) or passed with `--cas-id`.

### Web UI

```bash
python -m src.app
```

Then **open http://127.0.0.1:5000**. The app opens on a four-step flow, one screen each:

| Step | Screen | What happens |
|---|---|---|
| 1 | `/` | Upload the roster spreadsheet and the packets (a ZIP, the PDFs themselves, or a mix). Nothing is summarized yet. |
| 2 | `/step/2` | Download the same spreadsheet with `undergrad_degree` filled in and a blank `status` column. Mark it up in Excel. |
| 3 | `/step/3` | Upload the marked-up sheet (plus more packets if you have them). Merges on `cas_id`. |
| 4 | `/triage` | The swipe deck — one card per `tbd` applicant, left rejects, right shortlists. |

Each step has a Next button and the header shows where you are; nothing is summarized before step 3, so the first screen stays a plain upload page.

Uploads run in the background and report progress ("reading packet 7 of 31"), which matters because a batch of scanned packets is minutes of OCR. Assigning a professor, editing the faculty list and clearing data all save in place with a toast instead of reloading. Both fall back to plain form posts if JavaScript is off — the routes still redirect and flash as they always did. The applicant list and the professor queues have a search box, status filters and A–Z / GPA sorting, all client-side.

`/applicants` is the browse view off to the side: every applicant on file, decided or not, with the per-student pages, quarantined rows and data-to-verify flags. "Clear all" lives there — it resets derived records; tick "also delete raw uploads" to purge original files from disk.

### The review pass (accept / reject / tbd)

The intended working order, so nobody reads a card for an applicant who's already decided:

**Option A — from a folder (no browser):** put the roster spreadsheet and the packets (a ZIP, loose PDFs, or both) in one folder, then:

```bash
./run_local.sh review          # prompts you to drag the folder onto the window
./run_local.sh review "/path/to/folder"
python -m src.enrich --folder "/path/to/folder"
```

It writes `<roster>_review.xlsx` **next to your original** — which is only ever read, never modified — and prints how many degrees it could fill, how many applicants had no packet, and how many had a packet that never names a bachelor's. Skip to step 3.

**Option B — from the app:**

1. Upload the roster **and** the packets (ZIP or loose PDFs) — the degree is read out of the packets, so this step needs both.
2. Click **⬇ Review spreadsheet** (or `./run_local.sh review`). You get `applicants_review.xlsx`: your own spreadsheet, every column intact, with `undergrad_degree` filled in and a blank `status` column right after the names.
3. Mark each row `accept`, `reject`, or `tbd` in the **`status`** column (dropdown provided). Blank means undecided. Save.
4. Upload that file again on the home page. It merges on `cas_id` — nobody is duplicated.
5. **Summaries** now lists only the `tbd` applicants, and so does the `.md` download.

Re-uploading the original roster (no `status` column) won't erase anything already recorded — only a recognized value overwrites one. Until at least one applicant is marked, summaries show everyone.

### GPA: what's shown

The figure on a card is **the number your spreadsheet holds** — 3.36, 9.14, 72 —
not a conversion. A denominator (`3.36/4.0`) appears only when the scale is
known, which means you set it:

```yaml
# config/csv_field_map.yaml
gpa:
  gpa_scale: 4.0        # or 5.0 / 10.0 / 100.0; null = infer per value
```

Left as `null`, the scale is inferred from the magnitude (≤4.3 → /4, ≤5 → /5,
≤10 → /10, ≤100 → percentage). That's unambiguous for a 9.14 or a 72, and a coin
flip in the 0–4.3 band where a /5 transcript overlaps a /4 one — so the card says
"scale not stated in the sheet" and shows no conversion rather than asserting
one. The inferred value still exists behind the scenes for the GPA sort, because
it's the only figure comparable across scales.

Set the scale and re-ingest and the flag clears; the denominator and the ≈/4.0
conversion come back.

### Two roles, one app

Login is **off until you create an account**. A single reviewer on a laptop shouldn't
have to authenticate to themselves, so with no accounts everything works as before.
The moment `config/users.yaml` has someone in it, every page needs a session.

```bash
python -m src.users demo --password <choose-one>      # advisor + one login per professor
python -m src.users add --username maya --password ... --role advisor
python -m src.users add --username pgratz --password ... --role professor \
                        --professor gratz-paul        # id from config/professors.yaml
python -m src.users list
python -m src.users remove --username pgratz
```

Passwords are stored as salted PBKDF2 hashes, never in the clear. The demo command
gives every account the same password — change them before this leaves your machine.

| Role | Lands on | Can reach |
|---|---|---|
| `advisor` | `/` dashboard | everything: the four-step flow, triage, queues, the faculty list |
| `professor` | `/prof` their deck | only the students assigned to them |

A professor opening an advisor URL gets a **403**, not a quiet redirect — being bounced
without explanation hides the fact that the link they were sent isn't for them. They also
can't act on a student outside their own queue even with the id in hand.

### Asking for an account

The sign-in page has a **Create account** button. It creates a *request*, not access:
the person picks a username and password, and the account sits with `role: pending`
until an advisor approves it under **Accounts** and says what it is. Three details are
deliberate, and worth leaving alone:

- **Pending is stored as the role**, not as an `approved: false` flag next to a real
  one. Every gate in the app asks "are you advisor/professor?", so a pending account
  fails all of them by construction. A flag would mean an account that already holds
  real access and is only held back by a check someone could forget to write.
- **Pending accounts don't count towards "login is on."** If they did, a stranger
  signing up on an app in no-login mode would flip login on — locking out the advisor,
  who by definition has no account yet, with only the CLI to recover.
- **Signup says the same thing whether or not the name was taken.** "That username
  exists" on a form anyone can reach is a staff directory.

The requester never picks their own role; the advisor sets it at approval. Reject
removes the request. From the CLI:

```bash
python -m src.users list                                  # shows pending separately
python -m src.users approve --username newprof --role professor \
                            --professor duffield-nick
```

**The professor's screen** is the same swipe deck: right accepts, left passes, `U` undoes,
and their queue is listed underneath with each verdict. Their call is stored separately
from the advisor's (`professor_status` vs `triage`), so "advisor shortlisted, professor
passed" stays visible instead of one overwriting the other.

**The status report** — `⬇ Status report (.xlsx)` on the dashboard, advisor only. One row
per applicant (everyone, not just the decided) with every verdict side by side: the
spreadsheet's `status`, your `advisor_triage`, the professor's name and their call, plus
degree, GPA, areas, flags and which documents are on file. A second sheet tallies each
professor. Values rather than formulas — it's a snapshot of where things stood when you
took it, and a cell that recalculated later would misrepresent that.

**The advisor's dashboard** (`/`) is professor-wise: counts across the top, then each
professor with their assigned students and what they said — accepted, rejected, or still
waiting — plus a "not assigned yet" pile for shortlisted students in nobody's queue.

### The swipe pass (triage → professor queues)

With applicants marked `tbd`, click **⇄ Triage** on the home page (or open `/triage`).

- One card per applicant, sized to a single screen: header, background, top experience, a project, one SOP quote, letters. Warnings and sources show as chips; the name links to the full record.
- **Drag the card**, use the buttons, or press <kbd>←</kbd> reject / <kbd>→</kbd> shortlist. <kbd>U</kbd> undoes the last swipe.
- A **left** swipe also sets `status` to `reject`, so they leave the tbd pool. A **right** swipe only shortlists — the spreadsheet's own verdict is untouched.
- **Summaries** and the `.md` download carry the applicants you swiped — shortlisted first, then rejected — not everything the pipeline extracted. A summary is the record of a read that happened; someone still in the deck hasn't been read. The download is rebuilt on the way out, because swiping changes what belongs in it without re-running the pipeline.
- When the deck empties, open the **Shortlist** and give each applicant to one professor. **Professor queues** (`/professors`) shows who's waiting on whom.

Faculty live in `config/professors.yaml` (`id`, `name`, optional `url` and `areas`), editable two ways:

- **In the app** — *Professor queues* → **✎ Edit professors**: add, rename, change a link or areas, delete. Writes the same YAML file.
- **By hand** — edit the file; the app picks it up on the next page load.

The `id` is generated once from the name and never rewritten, so renaming a professor keeps their queue. Deleting one sends anyone still in their queue back to the shortlist as unassigned, rather than leaving records pointing at an id that's gone. Profile links must be `http://` or `https://`.

Environment variables (all optional; defaults are the safe choice):

| Env var | Default | Purpose |
|---|---|---|
| `GRADAPP_HOST` / `GRADAPP_PORT` | `127.0.0.1` / `5000` | Bind address/port. Non-loopback requires `GRADAPP_ALLOW_REMOTE=1` **and** `GRADAPP_PASSWORD`. |
| `GRADAPP_PASSWORD` | unset | Gates every route behind HTTP Basic Auth. |
| `GRADAPP_SECRET_KEY` | random per process | Cookie signing key. |
| `GRADAPP_DEBUG` | off | `1` enables the Werkzeug debugger (never on a reachable host). |
| `GRADAPP_MAX_UPLOAD_MB` | `200` | Request-body size cap. |
| `GRADAPP_FORCE_SECURE_COOKIES` | off | `1` marks cookies `Secure` (behind TLS only). |
| `GRADAPP_OCR_ENGINE` | `tesseract` | `rapidocr` selects RapidOCR if installed. |

Built-in guardrails (not configurable): upload extension allow-lists + magic-byte checks, per-form CSRF tokens + same-origin checks, rate limiting, security response headers, access log at `data/audit.log`. Full detail in README §Security.

### Tests & lint

```bash
pytest -q                                                    # full suite
python -m pyflakes src                                       # lint
pytest --cov=src --cov-report=term-missing --cov-fail-under=80   # what CI runs
python tests/adversarial.py /tmp/adv-scratch                 # hostile-input pass
```

`tests/loadtest.py` generates N synthetic applicants and times every page:

```bash
python tests/loadtest.py 5000 /tmp/load-scratch
```

At 5000 applicants on a laptop: ingest ~12s, every page under 0.6s and 0.9 MB,
both .xlsx exports ~2.5s. It flags anything over 3s or 3MB. Four things had to
change to get there, all of them the same mistake in different clothes — doing
per-applicant work for the whole cohort on every request:

| Was | Why | Now |
|---|---|---|
| `/applicants` 19 MB | a full summary card per applicant | 50 to a page |
| `/triage` 8.8 MB | every tbd card in the DOM | batches of 25, fetched as you swipe |
| status export 11.6s | `ws[row]` rescans the sheet for its width — quadratic | one `iter_rows` pass |
| degree + coverage | re-parsed every packet per page view | cached on the index at write |

Two ingest paths were quadratic too: the near-duplicate SOP check read every
full record per document (now a MinHash sketch on the index, with the top
candidates confirmed exactly), and single-document ingest re-ran reconciliation
and rewrote the whole report each time (now batch-only).

`tests/adversarial.py` is deliberately outside the pytest suite — it writes junk
files, hammers the store from a dozen threads and pokes the CSRF layer. Give it
a scratch directory, never `data/`. It probes hostile uploads (renamed
executables, zip-slip, 500-character filenames, fake PDFs, latin-1 CSVs), XSS
and YAML injection, status-column noise, route abuse, CSRF and cross-origin
posts, concurrent swipes and uploads, background-job failures, degree-extraction
traps and review-sheet edge cases.

### Regenerate the JSON schema

```bash
python -m scripts.gen_schema     # rewrites student.schema.json from the Pydantic model
```

## 6. File-by-file reference

### Root

| File | Contents |
|---|---|
| `README.md` | Project overview, quick-start commands, column mapping, security guardrails. |
| `RUNBOOK.md` | This document. |
| `ARCHITECTURE.md` | Full design doc: principles, both ingestion paths, merge/storage design, quarantine vs. flags, project layout, out-of-scope list. |
| `architecture.mermaid` / `.svg` / `.png` | Pipeline diagram (mermaid source + rendered exports). |
| `student.schema.json` | JSON Schema for the canonical record — **generated** from `src/models.py` via `scripts/gen_schema.py`; never edit by hand. |
| `requirements.txt` | Runtime dependencies (pydantic, pandas, Flask, pypdf, OCR stack, …). |
| `requirements-dev.txt` | Test/lint tooling: pytest, pytest-cov, reportlab, pyflakes. |
| `constraints.txt` | Pinned versions of every runtime dependency for reproducible installs. |
| `pytest.ini` | Pytest config: `testpaths = tests`, quiet mode, warning filters. |
| `.coveragerc` | Coverage config: measure `src/`, omit `add_document.py`, exclude `main()`/`__main__` blocks. |
| `.gitignore` | Excludes `data/` (all applicant PII), `.env` secrets, Python/editor cruft. |
| `.github/workflows/ci.yml` | GitHub Actions: on push/PR, matrix Python 3.10–3.12, installs Tesseract, pyflakes lint, pytest with 80% coverage gate, uploads coverage.xml. |

### `src/` — the pipeline

| File | Contents |
|---|---|
| `__init__.py` | Package docstring, `SCHEMA_VERSION = "2.0.0"` (canonical record), silences noisy pypdf warnings. |
| `run.py` | CLI orchestrator: parse → normalize → validate → merge → store → summary; writes `data/reports/_summaries.md`; exposes `reindex()`. |
| `app.py` | Flask web UI. Routes for upload (CSV/Excel, ZIP, single documents), browsing/downloading summaries, quarantine view, reset/purge. Implements all security guardrails (CSRF, rate limits, Basic Auth, headers, audit log). No pipeline logic — orchestrates `src.run`. |
| `parse.py` | Stage 3, deterministic extraction: reads CSV **or** Excel via pandas into `{column: value}` dicts; cleans Excel float artifacts (`1001.0` → `1001`); collapses indexed columns (`name_0`, `name_1`, …) into grouped arrays. |
| `normalize.py` | Stage 4a: assembles the canonical record from a parsed row using candidate column-name lists (IELTS/TOEFL/GRE/education/programs/interests), normalizes GPA to a 4.0 scale with per-value auto-detection, records data-quality warnings. Deliberately never maps protected attributes. |
| `models.py` | Typed Pydantic v2 canonical model (`StudentRecord` + sub-models: GPA, EducationEntry, test scores, programs…). `extra="forbid"` everywhere; range checks; source of truth for `student.schema.json`. |
| `validate.py` | Thin wrapper exposing `validate_record(record) -> (is_valid, errors)` over `models.parse_record`. |
| `template.py` | Stage 5: renders the factual per-applicant **summary card** — a three-line header (identity, the numbers, the program), then short `###` sections (Background, Experience, Projects, Notable, In their words, Letters, On file, Verify) with one clipped item per line. Bounded rather than terse — a bullet that stops at "in order to obtain better" has told the reviewer nothing; clip budgets and per-section item counts live at the top of the file (`EXCERPT`, `QUOTE`) and in `triage._CARD_SECTIONS` for the deck. Valid Markdown, re-rendered as headed sections by `templates/_summary.html`. No scoring, nothing invented. |
| `triage.py` | Stage 7, the swipe pass: picks the deck (tbd, not yet swiped), the swipe/undo semantics (left also rejects, right only shortlists), `condense_card()` for the one-screen card, and the professor list + queues from `config/professors.yaml`. |
| `review.py` | Stage 6, the review pass: pulls the undergrad degree out of the packet (never inferred from roster columns), normalizes the reviewer's accept/reject/tbd `status`, narrows the summaries to `tbd`, reports degree coverage, and writes the review workbook (openpyxl: dropdown, frozen header, legend sheet). |
| `enrich.py` | CLI driver for step 1 from a folder (`python -m src.enrich --folder …`): finds the roster spreadsheet, ingests the ZIPs and loose PDFs beside it, and writes `<roster>_review.xlsx` next to the original. Reads the roster, never writes it. |
| `merge.py` | Unified per-student record (schema v3.0.0): identity + `sources.{application,resume,sop,lors[]}` + provenance + warnings. `upsert_source()` is the single extension point for new source types. |
| `store.py` | SQLite store for unified records, keyed by `cas_id`. Atomic writes, `BEGIN IMMEDIATE` read-modify-write, WAL mode, graceful degradation on filesystems without POSIX locks. |
| `documents.py` | Phase-2 single-document ingestion: extracts text from PDF/DOCX/TXT/MD, builds a source section (word counts, analysis, recommender), merges by `cas_id`. Defines `DOC_TYPES = (sop, lor, resume)`. |
| `add_document.py` | CLI wrapper over `documents.py` (`python -m src.add_document --input … --type …`); infers `cas_id` from filename prefix. |
| `ingest_zip.py` | Batch packet ingestion of combined "Full Application" PDFs: validates the ZIP, enforces count/size limits, reads entries in memory (no zip-slip), segments each PDF, merges sections per student. `ingest_packet_folder()` runs loose PDFs in a folder through the same `_ingest_packets()` path. |
| `packet.py` | Segments a combined PDF into resume/SOP/LOR/scores/ignore sections: per-page feature scores (headings, contact block, first-person density, form/thesis detectors) decoded jointly with Viterbi transition penalties; flags low confidence. |
| `ocr.py` | Optional local OCR for scanned pages: pypdfium2 render → grayscale/autocontrast/Otsu/deskew → Tesseract (or RapidOCR). Every step degrades gracefully; OCR never raises. |
| `analyze.py` | Deterministic text analysis for SOPs/LORs: word-boundary keyword matching against `config/keywords.yaml` (research areas, skills), length/structure flags, test-score & recommender extraction, Jaccard near-duplicate detection. |
| `ratings.py` | Reads CAS "REFERENCE RATINGS" Likert tables (1–5 across eight criteria) from digital PDFs by mapping check-glyph x-positions to column anchors via pypdfium2. Skips scanned forms. |

### `config/`

| File | Contents |
|---|---|
| `csv_field_map.yaml` | Raw-column → canonical-field mapping: scalars, indexed groups (IELTS/TOEFL/education/experience), custom-question substring matches, and GPA scale settings (`gpa_scale: null` = auto-detect, with the threshold table). Edit when the export layout changes. |
| `keywords.yaml` | Keyword taxonomy for SOP/LOR analysis: 10 research areas (Power Systems … Photonics), a skills list, and thresholds (e.g. `sop_min_words`). Edit for your department. |

### `scripts/`

| File | Contents |
|---|---|
| `gen_schema.py` | Regenerates `student.schema.json` from the Pydantic model: `python -m scripts.gen_schema`. |

### `templates/` — Jinja2 (Flask UI)

| File | Contents |
|---|---|
| `base.html` | Shared layout: nav, flash messages, CSP-compatible styling. |
| `index.html` | Dashboard: upload forms (CSV/Excel, ZIP, single document), student list, quarantine, clear/purge controls. |
| `student.html` | Per-student detail: summary, sources, ratings, data-to-verify flags. |
| `summaries.html` | All summaries in one page / download view. |

### `tests/`

| File | Contents |
|---|---|
| `test_pipeline.py` | Core pipeline suite: parsing (CSV/Excel edge cases), normalization, GPA scales, validation/quarantine, merge, packet segmentation (reportlab-generated PDFs), OCR path, storage. |
| `fixtures/sample_applications.csv` | Committed synthetic demo applicants used by tests and the README quick-start. Tests must never read from `data/` (gitignored → absent on CI). |
| `test_app_guardrails.py` | 24 security tests for the Flask UI: extension/magic-byte rejection, 413 cap, same-origin 403, CSRF, rate-limit 429, Basic Auth, audit log, error logging, purge, protected-attribute enforcement. |
| `test_fixes.py` | Regression tests for specific fixed bugs. |

### `data/` — local only, **gitignored**, never committed

`raw/` (uploaded CSV/Excel/ZIP/PDF originals), `students.db` (+ `-wal`) SQLite store, `reports/_summaries.md` generated report, `quarantine/` invalid rows, `audit.log` web-UI access log.

## 7. Common operations & troubleshooting

- **Re-running is safe**: pipelines merge into existing records by `cas_id` (upsert), they don't overwrite.
- **Wrong GPA scale?** Force one via `gpa.gpa_scale` in `config/csv_field_map.yaml`.
- **Scanned PDF pages come back empty**: install Tesseract (see §4); pages are flagged, not lost.
- **"disk I/O error" on a synced/network drive**: `store.py` auto-degrades WAL → journal → in-process lock; prefer a local disk.
- **Changed `src/models.py`?** Regenerate the schema: `python -m scripts.gen_schema`.
- **New CSV layout?** Update `config/csv_field_map.yaml` and/or the candidate lists in `normalize.py`.
