#!/usr/bin/env bash
# Grad App — start the local web UI.
#
#   ./run_local.sh          # web UI (auto-picks a free port, opens browser)
#   ./run_local.sh cli      # CLI on the sample fixture instead
#   ./run_local.sh review   # write data/reports/applicants_review.xlsx -- the
#                           # roster plus undergrad_degree and a blank status
#                           # column to mark accept / reject / tbd
#   ./run_local.sh refresh  # re-render every stored summary with the current
#                           # template (needed after editing src/template.py --
#                           # summaries are cached in the record, not live)
#
# Creates/reuses .venv, installs pinned deps on first run, then starts Flask.
set -uo pipefail
cd "$(dirname "$0")"

fail() { echo; echo "!! $*"; echo; read -r -p "Press Return to close." _; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 not found. Install it from python.org or via 'brew install python@3.12'."

if [ ! -d .venv ]; then
  echo "==> Creating virtualenv (.venv)"
  python3 -m venv .venv || fail "Could not create .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

PYVER="$(python -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "==> Python $PYVER in $(pwd)/.venv"
case "$PYVER" in
  3.10|3.11|3.12) ;;
  *) echo "    note: this project is tested on 3.10-3.12; you're on $PYVER." ;;
esac

if ! python -c "import flask, pydantic, pandas, pypdf, pypdfium2, docx, yaml, openpyxl" >/dev/null 2>&1; then
  echo "==> Installing pinned dependencies (first run takes a minute)"
  pip install --quiet --upgrade pip
  pip install -r requirements.txt -c constraints.txt || fail "Dependency install failed (scroll up for the error)."
fi

if [ "${1:-web}" = "refresh" ]; then
  echo "==> Re-rendering all stored summaries with the current template"
  python -c "from src.run import reindex; n=len(reindex('data', changed=None)); print(f'    refreshed {n} student(s)')" \
    || fail "Refresh failed (scroll up)."
  echo "==> data/reports/_summaries.md rewritten. Restart the web UI to see it."
  read -r -p "Press Return to close." _
  exit 0
fi

if [ "${1:-web}" = "review" ]; then
  FOLDER="${2:-}"
  if [ -z "$FOLDER" ]; then
    echo "Drag the folder holding the spreadsheet and the packets onto this window,"
    echo "then press Return. (Or just press Return to use the applicants already loaded.)"
    read -r -p "Folder: " FOLDER
    # A drag-and-drop into Terminal arrives quoted or backslash-escaped.
    FOLDER="$(printf '%s' "$FOLDER" | sed -e "s/^['\"]//" -e "s/['\"][[:space:]]*$//" -e 's/\\ / /g')"
  fi

  if [ -n "$FOLDER" ]; then
    echo "==> Reading $FOLDER"
    python -m src.enrich --folder "$FOLDER" || fail "Could not build the review spreadsheet (scroll up)."
  else
    echo "==> Building the review spreadsheet from the applicants already on file"
    python -c "
from src.review import write_review_sheet, degree_coverage
from src.store import Store, db_path_for
recs = Store(db_path_for('data')).all()
if not recs:
    raise SystemExit('No applicants on file yet — upload a roster, or re-run with a folder.')
print('    ' + write_review_sheet(recs, 'data/raw', 'data/reports/applicants_review.xlsx'))
c = degree_coverage(recs)
print(f\"    undergrad_degree: {c['with_degree']} of {c['total']} filled in\"
      + (f\", {c['no_packet']} with no packet on file\" if c['no_packet'] else ''))
" || fail "Could not build the review spreadsheet (scroll up)."
  fi
  read -r -p "Press Return to close." _
  exit 0
fi

if [ "${1:-web}" = "cli" ]; then
  echo "==> Running pipeline on tests/fixtures/sample_applications.csv"
  python -m src.run --input tests/fixtures/sample_applications.csv || fail "Pipeline failed (scroll up)."
  echo "==> Report written to data/reports/_summaries.md"
  read -r -p "Press Return to close." _
  exit 0
fi

# Port 5000 is taken by macOS AirPlay Receiver on most Macs, so start at 5001
# and walk upward until we find one nothing is listening on.
PORT="${GRADAPP_PORT:-}"
if [ -z "$PORT" ]; then
  for p in 5001 5002 5003 5050 8000 8080; do
    if ! lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then PORT="$p"; break; fi
  done
fi
[ -n "$PORT" ] || fail "Could not find a free port. Close some apps and retry."

URL="http://127.0.0.1:$PORT"
echo "==> Starting web UI at $URL   (Ctrl-C to stop)"
( sleep 2; open "$URL" >/dev/null 2>&1 || true ) &

export GRADAPP_PORT="$PORT"
python -m src.app || fail "The app exited unexpectedly (scroll up for the traceback)."
