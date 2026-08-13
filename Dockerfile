# Container image for Cloud Run.
#
# Two things drive the shape of this file:
#
#  * Tesseract. OCR is a system binary, not a pip package. Without it the app
#    still runs — ocr.py degrades and flags scanned pages instead of failing —
#    but every scanned packet arrives unreadable, which is most of them.
#  * gunicorn with threads, not processes. The ingest worker is a thread that
#    holds progress in memory for the life of the request cycle; forking
#    workers would mean the poll can miss the worker that owns the job even
#    within one container. One worker, several threads, and the shared jobs
#    table covers the across-instance case.

FROM python:3.12-slim

# tesseract-ocr: scanned packets. libgl1/libglib: image handling for the
# preprocessing pipeline (Pillow/numpy) used before OCR.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first: this layer is cached unless the dependencies change, so
# an ordinary code change rebuilds in seconds rather than reinstalling pandas.
COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY src/ ./src/
COPY templates/ ./templates/
COPY config/ ./config/

# Scratch space. On Cloud Run this is a tmpfs living in the instance's memory
# and is erased on restart — which is exactly why storage.py mirrors everything
# to the bucket. Nothing here is the source of truth.
ENV GRADAPP_DATA=/tmp/gradapp-data
RUN mkdir -p /tmp/gradapp-data

ENV GRADAPP_ENV=cloud

# Cloud Run sends $PORT and expects the app to listen on it.
ENV PORT=8080
EXPOSE 8080

# --timeout 0: an ingest of scanned packets legitimately runs for minutes, and
# gunicorn's default 30s would kill the request mid-OCR.
# --threads 8: concurrency without forking, per the note above.
CMD exec gunicorn --bind :$PORT \
    --workers 1 --threads 8 --timeout 0 \
    --access-logfile - --error-logfile - \
    src.app:app
