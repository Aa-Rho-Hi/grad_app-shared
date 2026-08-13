"""CLI to ingest a phase-2 document (SOP / LOR / transcript) into a student.

    python -m src.add_document --input "C1234_sop.pdf" --type sop
    python -m src.add_document --input letter.pdf --type lor --cas-id C1234 --recommender "Dr. X"

cas_id is inferred from the filename prefix ("<cas_id>_..."), or pass --cas-id.
"""
from __future__ import annotations

import argparse
import json
import os

from .documents import DOC_TYPES, infer_cas_id, ingest_document
from .run import reindex

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest an SOP/LOR/transcript document")
    ap.add_argument("--input", required=True, help="path to .pdf/.docx/.txt")
    ap.add_argument("--type", required=True, choices=DOC_TYPES)
    ap.add_argument("--cas-id", default=None, help="student cas_id (else inferred from filename)")
    ap.add_argument("--recommender", default=None, help="LOR author (optional)")
    ap.add_argument("--outdir", default=os.path.join(ROOT, "data"))
    args = ap.parse_args()

    cas_id = args.cas_id or infer_cas_id(args.input)
    if not cas_id:
        raise SystemExit("Could not determine cas_id. Pass --cas-id, or name the "
                         "file '<cas_id>_<type>.pdf'.")
    _, words = ingest_document(args.input, cas_id, args.type, args.outdir,
                               recommender=args.recommender)
    reindex(args.outdir, changed={cas_id}, reconcile=False)
    print(json.dumps({"cas_id": cas_id, "type": args.type, "words": words,
                      "outdir": args.outdir}, indent=2))


if __name__ == "__main__":
    main()
