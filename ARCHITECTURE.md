# Graduate Applicant Pipeline — Architecture

## What it is

A fully **local, deterministic** pipeline that turns graduate-application data
(CSV/Excel) and supporting documents (resumes, SOPs, LORs — as single files, one
combined "Full Application" PDF, or a ZIP of many packets) into **one validated
JSON record per student**, then a factual descriptive summary. It describes
applicants — it does **not** score, rank, or judge them, and it never sends data
to an LLM or over the network.

## Principles

- **Local & deterministic.** No LLM and no network calls anywhere. Same input always yields the same output.
- **One record per student**, keyed by `cas_id`. Application data, resume, SOP, and LOR(s) merge into the same record so everything about a student lives in one place; records keyed by email vs. `cas_id` are reconciled.
- **Describe, don't decide.** The output is a neutral summary; the reviewer makes the call.
- **Fairness by construction.** Protected attributes (race, gender, age, citizenship, phone) are never parsed into the record.
- **Strong validation.** A typed Pydantic model coerces, range-checks, and forbids unknown fields; bad rows are quarantined with readable reasons.
- **Durable & concurrency-safe.** Records live in a local SQLite store; each merge is an atomic, lock-safe transaction, so concurrent uploads can't corrupt or lose a record.

## Diagram

See `architecture.svg` / `architecture.png` (rendered) and `architecture.mermaid` (source).

Two ingest paths feed one record:

```
CSV/Excel ──────── parse ─ normalize ─ strong-validate ───────────┐
                                                                  ├─ merge by cas_id ─ reconcile ─ Unified Student JSON ─ summary ─ UI
SOP / LOR / Resume ─ extract text (OCR fallback) ──┐              │
Combined PDF / ZIP ─ extract text ─ segment ───────┴─ analysis ───┘
```

Documents reach the pipeline three ways: as a single file (SOP / LOR / resume),
as one combined "Full Application" PDF that the segmenter splits into sections,
or as a ZIP of many such packets processed in one batch.

## Application path (CSV/Excel → application section)

1. **`parse.py`** — reads CSV *or* Excel with pandas (robust to quoting, encodings, BOM, Excel numeric-ID quirks); collapses indexed columns (`name_0`, `name_1`, …) into arrays.
2. **`normalize.py`** — maps source columns to canonical fields via candidate name-lists (tolerant of different exports), picks the identifier (`cas_id` → `email` → generated), normalizes GPA to a 4.0 scale (scale auto-detected per value, since it varies by university), and records data-quality warnings. Protected attributes are dropped here.
3. **`models.py` + `validate.py`** — the strong parser (Pydantic v2): coercion, range checks, `extra="forbid"`. Valid rows become the **application** section; invalid rows go to `data/quarantine/`. The model generates `student.schema.json` (`scripts/gen_schema.py`).

## Document path (SOP / LOR / Resume → their own sections, local & no LLM)

1. **`documents.py`** — extracts plain text from PDF, DOCX, or TXT and packages it as a source section. Supported types: `sop`, `lor`, `resume`. Single-file ingest (`ingest_document`) infers the `cas_id` from the filename prefix (`<cas_id>_sop.pdf`) and, for SOPs, runs a near-duplicate check against every other applicant's SOP.
2. **`ocr.py`** — text extraction for PDFs, with an OCR fallback. A page is treated as scanned (and OCR'd) when its embedded text is too sparse to be a real text page — by **word count**, not a bare character threshold, because scanned pages routinely carry a little embedded text (a "Scanned by CamScanner" watermark, a page number, a header) that defeats a naive threshold while the real content is an image. The rule: OCR if the page has very few words, or has a raster image (detected quickly via the page's XObject resources, no decode) and still few words. Scanned pages are rasterized at 300 DPI with `pypdfium2`, cleaned (grayscale → autocontrast → Otsu binarization → small-angle deskew, all with safe fallbacks), and read with Tesseract (`pytesseract`, LSTM engine); the **longer** of the embedded vs OCR'd text is kept, so a good text page can never be made worse. OCR runs **in parallel across pages** (a process pool, since Tesseract is single-threaded per call) with a per-page timeout and a per-document page cap. If Tesseract isn't installed the pipeline still runs and flags the affected pages rather than failing. OCR usage is recorded on the record (not treated as an error), and OCR'd pages are surfaced as data-to-verify since recognition is never perfect.
3. **`packet.py`** — admissions exports often bundle a student's resume, SOP, letters, score cards, transcripts and sometimes an entire **thesis** into **one** PDF. `segment()` labels every page with a small, deterministic sequence model (no LLM): each page gets an independent **emission score** for every label (resume / sop / lor / scores / ignore) from a bundle of features — heading phrases, a contact block, multi-word first-person prose density, LOR/reference form fields **and narrative recommendation-letter cues** (salutation/closing + recommend verbs + third-person about the applicant, so a first-person reference letter isn't mistaken for an SOP), score-report & transcript cues, and form/thesis/math detectors — and the page sequence is then **Viterbi-decoded** with transition penalties so sections come out as contiguous blocks. This replaced an earlier "carry the previous label forward" rule that dumped theses and form pages into whatever section preceded them. Resume/SOP/LOR/scores are kept; forms, boilerplate and thesis/appendix pages go to `ignore` and are dropped. A section that ends up spanning non-adjacent pages is flagged for review.
4. **`ratings.py`** — reads the CAS **"REFERENCE RATINGS" Likert tables** (Average=1 … Exceptional=5). The rating is a check-glyph whose *column* — and therefore value — is lost by plain text extraction, so this reads the page with character **positions** (pypdfium2): it finds the column x-anchors from the "(1)…(5)" markers (rejecting phone numbers/dates that merely contain those digits via an even-spacing test), then maps each check-glyph's x to its column. It returns each evaluator's name, per-criterion ratings, average, and recommendation phrase, plus the **average across all evaluators**. Works on digital CAS forms; a scanned evaluation has no glyph positions and is skipped.
5. **`ingest_zip.py`** — batch-ingests a **ZIP of combined packets** in one pass: `cas_id` (and a best-effort name) come from each filename, each PDF is segmented and analyzed, the Likert ratings are read and attached to the LOR source, and sections merge into the right student record. Safe by design — validates the archive, enforces file-count/size limits, and reads each entry in memory (no disk extraction → no zip-slip).
5. **`analyze.py`** — deterministic analysis of the extracted text:
   - **research areas** and **skills/tools** detected via the keyword taxonomy in `config/keywords.yaml`;
   - **length/structure** stats (word/sentence counts, avg sentence length);
   - **quality flags** (too short, no recognized area, LOR missing a recommender);
   - **near-duplicate SOP detection** across applicants (trigram Jaccard) to flag possible reuse;
   - **motivation sentences** quoted verbatim from an SOP (the applicant's own words — selected, never interpreted);
   - **recommender extraction** from LOR text (the CAS `REFERENCES` block, signature closings, or a `Professor/Dr.` fallback) — name, title, and organization;
   - **test-score extraction** from two sources, reported as written: ETS-style score reports (`extract_test_scores`) and the CAS academic-history "STANDARDIZED TESTS" tables (`extract_standardized_tests`, which reads the GRE/IELTS/TOEFL value rows, stripping interleaved percentile columns). These fold into the academic-standing line marked "(parsed)".

   This is keyword + structure + pattern extraction, **not** interpretation of the writing.

   Score reports and transcripts (the scanned CamScanner pages, GRE/TOEFL reports) are **no longer dropped**: they are OCR'd, captured as a `scores` source, and their parsed figures surfaced in the summary (flagged "verify against official records").

## Merge & the unified record (`merge.py`)

Each source is attached to the student's record by `cas_id`:

```json
{
  "schema_version": "3.0.0",
  "student_id": "<cas_id>",
  "identity": { "full_name": "...", "email": "..." },
  "sources": {
    "application": { "programs": [...], "education": [...], "english_proficiency": {...}, "gre_results": [...], "interests": {...} },
    "resume": { "text": "...", "word_count": 0, "analysis": {...} },
    "sop":  { "text": "...", "word_count": 0, "excerpt": "...", "analysis": { "detected_areas": [...], "mentioned_skills": [...], "flags": [...] } },
    "scores": { "text": "...", "analysis": { "test_scores": { "gre_verbal": 155, "gre_quant": 168, "gre_awa": 4.5, "gre_total": 323 } } },
    "lors": [ { "text": "...", "recommender": "...", "recommender_title": "...", "recommender_organization": "...", "analysis": {...} } ]
  },
  "provenance": [ { "source": "application", "file": "...", "row": 0, "ingested_at": "..." } ],
  "warnings": [ "..." ]
}
```

Merging is additive: a new file fills missing fields without wiping what a prior
file set; different source types live in separate sections so they never collide;
LORs append. Re-running, or running a different source file, updates the same
record on disk.

**Reconcile (`merge.reconcile`).** The CSV may key a student by `email` (when the
export has no `cas_id` column) while the PDF packet keys the same person by their
numeric `cas_id`. After ingest, `reconcile()` folds the email/`ROW`-keyed record
into the matching `cas_id`-keyed one when their names agree. It is deliberately
conservative: it only collapses non-`cas_id` keys into a matching `cas_id` key,
never two distinct `cas_id`s. This runs automatically inside `reindex()` after any
source is ingested.

## Storage (`store.py`)

Records persist in a local **SQLite** database (`data/students.db`) — one row per
student, the unified record kept as JSON, plus a quarantine table. SQLite is an
in-process file database, so the pipeline stays fully local with no server or
network. Two properties matter:

- **Atomic writes.** A record is written in a single transaction, so a crash
  mid-write can never leave a half-written record (the earlier one-JSON-file-per-student
  writer could).
- **Concurrency-safe merges.** Every ingest path mutates a record through
  `Store.update(cas_id, fn)`, which opens a `BEGIN IMMEDIATE` (write-lock)
  transaction, reads the current record, applies the change, and writes it back
  under the same lock — plus a per-path in-process lock. Concurrent writers to the
  same student therefore serialize instead of overwriting each other ("lost
  update"). WAL mode keeps the web UI readable while a write is in flight, and
  `busy_timeout` makes contending writers wait rather than fail.
- **Filesystem robustness.** On a normal local disk the store uses WAL. Some
  networked/synced/FUSE mounts can't provide WAL's shared memory or POSIX locks
  (writes raise "disk I/O error"); the store probes this at startup and degrades
  WAL → rollback journal → a nolock mode where the in-process lock provides
  serialization. If a directory can't host SQLite at all, the database is
  relocated to a local working directory (the human-readable report still writes
  to the chosen folder) and a one-line notice is printed.

## Scale

Designed for one intake at a time, and measured rather than assumed —
`tests/loadtest.py` builds a synthetic cohort and times every page. The rule
that keeps it honest: **a request may not do per-applicant work across the whole
cohort.** Three mechanisms enforce it.

- **The index is the read model.** `Store._index_entry` caches everything a list
  view or an export needs — name, email, metrics, the derived undergrad degree,
  which documents exist, the originating roster file, and a MinHash sketch of the
  SOP. Pages and exports read that; full records (which carry whole essays) are
  only loaded for one applicant at a time.
- **Lists are paginated, decks are batched.** 50 cards a page; 25 in a deck with
  more fetched as the reviewer works down.
- **Batch operations stay batch.** Reconciliation and the report rebuild run once
  per ingest, not once per document.

Where it would stop: SQLite on one machine, single process. The quadratic paths
are gone but the duplicate-SOP scan is still O(n) per document — fine for a few
thousand, wants a real index past that.

## The review pass

Summarizing every applicant is wasted reading when most of them are decided on
the numbers. The flow puts a human triage step in the middle:

1. **Enrich** — `review.write_review_sheet` writes `reports/applicants_review.xlsx`:
   the reviewer's own roster, every column intact, with `undergrad_degree` filled
   in from the packet and a blank `status` column (accept / reject / tbd
   dropdown) inserted after the identity columns. The original upload is never
   modified. A "How to review" sheet carries the legend — a sample row would be
   re-ingested as a real applicant on the way back in.
2. **Decide** — the reviewer marks each row in Excel and re-uploads the file
   through the normal upload path.
3. **Narrow** — `review.status_from_row` reads the column back onto the
   record (stored as `review_status`, so it never collides with the roster's own
   `local_status_*`); the summaries page and `_summaries.md` then show only `tbd`
   applicants. Before anyone is marked nothing is hidden, so a roster that
   hasn't been reviewed yet still behaves as it always did.

`undergrad_degree` reads the packet only — the résumé's parsed education block,
then the résumé text, then the transcript — and prefers a line that names the
field of study over one that just names the credential ("B.Tech (Electrical
Engineering)" over "Bachelor of Eng."). The roster's `custom_field_background`
is a field of study, not a degree, and is blank for a third of the sample rows;
filling the column from it would put a guess where a reviewer reads fact, so
with no packet the cell stays empty.

## The swipe pass and the review groups

After the spreadsheet narrows the pool to `tbd`, `triage.py` is the second read:
one card per applicant, sized to a single screen, swipe right to shortlist and
left to reject (`/triage`). What survives is sent to one **review group**
(`groups.py`, `config/groups.yaml`), and the group takes it from there:

```
grad office --send to group--> group leader --assign--> reviewer (member)
                                     ^                        |
                                     +---- final call <-- first call
```

Each step keeps its own field, so disagreements stay visible:

| Field | Set by | Meaning |
|---|---|---|
| `review_status` | the spreadsheet's `status` column | accept / reject / tbd |
| `triage` | the grad office's swipe | shortlist / reject |
| `group` | the grad office (shortlist page or the card) | one group id |
| `assigned_to` | a group leader | one member's professor id |
| `professor_status` (+ `professor_note`) | that member: the first call | accept / reject |
| `final_status` (+ `final_note`, `final_by`) | a group leader: the final call | accept / reject |

Rules (`groups.py`, all unit-tested):

- A group is one or more leaders plus members, all professor ids from
  `config/professors.yaml`, so a rename never breaks a group. Leaders can also
  review.
- The final call needs the first call first. When the leader is the reviewer,
  one call counts as both.
- Moving an applicant upstream clears what was downstream: a different group
  starts that group's review over, a different reviewer drops the old first
  call. After the final call the reviewer is locked until the leader undoes it,
  and the member can't change their first call under it.
- "Split evenly" hands every unassigned applicant to the member with the fewest
  open reviews; area overlap breaks ties.
- Taking someone out of a group (or off the faculty list) sends their open
  reviews back to the leader; deleting a group sends its applicants back to the
  shortlist.

Who sees what. Three roles (`users.py`): **Grad office** (stored as
`advisor`), **Group leader** and **Professor**. `app.role_required(*roles)`
gates the grad office's pages; `app._leads` needs a Group leader login *and*
being named a leader of that group (a group can have several); `app._may_view`
lets the grad office open any applicant, a reviewer the ones assigned to them,
and a leader their groups'. The grad office can open a group's page but never
makes its calls, and can't stand in for a professor (`?as=` only works with no
logins). Roles are read back from users.yaml on every request
(`app.current_user`), so a role change or a removed account applies to open
sessions at once; with logins on, no user means no access, never all of it.
At least one Grad office login always remains. `tests/test_roles.py` walks
every route as every role and fails on any route without a role decision.
With no logins configured there is one operator who can do everything.

A left swipe writes both `triage=reject` **and** `review_status=reject`, so
the applicant leaves the tbd pool. A right swipe only sets `triage`, leaving the
spreadsheet's own verdict alone. `undo` restores `review_status` to tbd, which is
only safe because the deck contains nothing else.

`/summaries` and `reports/_summaries.md` show `triage.decided()` — the swiped set, shortlisted then rejected. Extracting a card for everyone is cheap; making a human read all of them is not, and an export nobody opens is worse than no export. The download is regenerated per request, since a swipe changes the report without re-running the pipeline.

The deck card is `triage.condense_card()` — a trim of the *rendered* summary
(header, then Background / Experience / Projects / In their words / Letters,
capped at a couple of items each), not a second renderer. One place decides what
a summary says; this only decides how much of it fits on screen.

## Output

- **`status_report.py`** — the grad office's .xlsx export: one row per applicant carrying every call (spreadsheet `status`, the swipe, the group and its leader, the reviewer's first call and note, the leader's final call and note), and a per-group tally sheet. `rows_for()`/`summarize()` are plain data so the shape can be tested without openpyxl. Values, not formulas: a snapshot that silently recalculated after an edit would misreport what was true when it was taken.
- **`users.py`** — accounts and the three roles (Grad office, Group leader, Professor). Login is off until `config/users.yaml` has an entry, which keeps the single-reviewer setup working and makes the switch explicit; passwords are PBKDF2 hashes. `advisor` reaches the whole flow, `professor` only their own queue, plus the workspace of any group they lead. Role checks return 403 rather than redirecting, and a professor's decision route re-checks `assigned_to` server-side so a copied link changes nothing.
- **`jobs.py`** — a one-process background job registry so a long ingest can report progress. The upload posts by fetch, the page polls `/jobs/<id>`, and the packet ingester calls back per PDF. Nothing is persisted: a job that dies with the process is work the reviewer re-runs, and the records it already merged are in the store either way.
- **`triage.py`** — the swipe pass: deck selection, swipe/undo semantics, the one-screen card, and the professor list (load/add/rename/delete, written back to `config/professors.yaml` atomically; ids are generated once and never rewritten so a rename can't empty a queue, and profile links are scheme-checked before they reach an `href`) with their queues.
- **`review.py`** — the review pass above: degree extraction, status parsing and normalization (`Accept`, `deny`, `T` all land somewhere sensible; anything unrecognized is "undecided", never a guess), the `tbd` filter, and the openpyxl writer for the review workbook.
- **`template.py`** — renders a neutral factual **summary card** per student from whatever sources are present (gets richer as documents are added): a three-line header (identity, the numbers, the program), then short `###` sections — Background, Experience, Projects, Notable, In their words, Letters, On file, Verify — with one clipped item per line. (Sections replaced the earlier one-`- Label:`-line-per-section layout, which joined a résumé's four experience bullets into a semicolon paragraph nobody could skim.) Surfaces SOP-detected areas/skills with an explicit "keyword detection, not interpretation" caveat. Long source sentences are clipped, never paraphrased — the full document text stays in the record. The text is valid Markdown, and `templates/_summary.html` re-renders the same plain text as headed sections for the web UI, so the `.md` download and the UI never diverge.
- **`app.py`** (Flask) — a four-step flow, one screen each: `/` upload (roster + packets: a ZIP, loose PDFs, or a mix), `/step/2` download the roster with the degree column and a blank `status`, `/step/3` take the marked-up sheet back, `/triage` the swipe deck. Nothing is summarized before step 3, so the first screen is a plain upload page rather than a wall of cards. `/applicants` keeps the browse view (every applicant, decided or not) off the flow. Also: attach a single SOP/LOR/resume per student; a `/reset` route to clear processed data (optionally purging raw uploads too); per-student detail (identity, programs, education, test scores, sources-on-file, document analysis); data-to-verify flags; quarantine view; written summaries; download. No login by default (single-reviewer, localhost tool) but ships with upload-size caps, extension allow-lists, a same-origin/CSRF check, and opt-in Basic Auth / remote-binding guards — see "Security & privacy guardrails" in `README.md`.
- **`run.py`** / **`add_document.py`** / **`ingest_zip.py`** — CLIs for the application path, single-document ingest, and ZIP-batch ingest. `run.py` also owns `reindex()` (rebuilds `students/_all.json` and `reports/_summaries.md`, and runs `reconcile`).

## Prescreen (`screening.py`, `prescreen_report.py`)

The grad office asked for rule-based flags on three tracks (Quick Admit on
campus, MS Distance, PhD Distance). This is the one place the app applies a
threshold, so it is built to stay honest about it:

- **Flags, not decisions.** Outcomes are only "Admit" or "Further review", and
  every non-admit lists its reasons. Anything a machine can't confirm (a
  letterhead, a signature, whether the advisor is TAMU faculty) goes in a
  "check by hand" list even on an Admit.
- **No guessed conversions.** The 3.25 rule is stated on a 4.0 scale; a GPA on
  another scale goes to further review rather than being converted. When an
  applicant's schools disagree across the threshold, a person picks.
- **Exact wording, robustly compared.** Letters are matched on a lower-cased,
  letters-and-digits-only form of the page so PDF spacing and quotes can't fail
  a correct letter, but every word must be present in order. A near miss
  (most of the wording in 3+ word runs) is reported as "close", never as met.
- **Evidence kept at ingest.** `screening.evidence_pages` keeps the packet pages
  with course codes or letter/advisor wording (`packet_evidence` on the record,
  capped at 60 pages) so rules can re-run without the PDFs.
- **Cached with a fingerprint.** `Store._index_entry` caches `profile` and
  `screening` on the index entry with a hash of the two config files;
  `Store.index()` recomputes entries cached under different rules, and
  `Store.refresh_index()` (the "Re-apply rules" button) persists them.

`prescreen_report.py` writes the three workbooks from index entries: values,
not formulas (applicant text starting with "=" is forced to text), a link to
the applicant's page on every row, a Summaries tab and a Rules tab.

## Data-quality flags vs. quarantine

- **Quarantine** — the row failed validation (e.g. an out-of-range value) and could not be recorded; shown with reasons.
- **Data-to-verify flag** — the record is valid but something is missing or unclear (e.g. "No GRE score on file", "SOP very short", "no cas_id — keyed by email"). Factual notes for the reviewer, not judgements.

## Project layout

```
grad app/
├── ARCHITECTURE.md · architecture.svg/.png/.mermaid · README.md
├── student.schema.json            # generated from src/models.py
├── config/
│   ├── csv_field_map.yaml          # column mapping + GPA scale rules
│   └── keywords.yaml               # research-area & skill taxonomy
├── scripts/gen_schema.py
├── src/
│   ├── parse.py  normalize.py  models.py  validate.py     # application path
│   ├── documents.py  analyze.py  ocr.py  packet.py        # document path (local, no LLM)
│   ├── ratings.py                                         # CAS Likert reference-rating reader
│   ├── ingest_zip.py                                      # batch ingest a ZIP of packets
│   ├── merge.py                                           # unified record + merge helpers
│   ├── store.py                                           # SQLite store: atomic upsert + reconcile
│   ├── template.py                                        # descriptive summary
│   ├── run.py  add_document.py                            # CLIs
│   └── app.py                                             # Flask web UI
├── templates/  (base, index, student, summaries)
├── data/  raw/ (uploads) · students.db (SQLite store) · reports/_summaries.md
└── tests/test_pipeline.py
```

## Deliberately out of scope

Genuine interpretation of SOP/LOR writing — assessing motivation, narrative
quality, or fit — would require a language model and is intentionally **not**
done, to keep the system local, private, and free of automated judgement.
