#!/usr/bin/env bash
#
# Deploy to Cloud Run. Run this yourself — it needs your gcloud login, and it
# creates billable resources.
#
#   ./deploy/deploy.sh my-project-id
#
# Idempotent: re-running it redeploys the code and leaves the database, bucket
# and secrets alone. Safe to run on every change.
#
# What it creates, and roughly what it costs a month:
#
#   Cloud SQL db-f1-micro    ~$8   runs 24/7 whether or not anyone uses the app
#   Cloud SQL storage 10GB   ~$2
#   Cloud Storage bucket     ~$0.20 for a few GB of packets
#   Cloud Run                 $0   scales to zero; a committee stays in free tier
#
# Admissions is seasonal and Cloud SQL is not. `gcloud sql instances patch
# NAME --no-activation-policy` stops it out of season and cuts that $8 to zero
# (you keep paying storage). See RUNBOOK.md.

set -euo pipefail

PROJECT="${1:?usage: deploy.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"

SERVICE=grad-app
DB_INSTANCE=grad-app-db
DB_NAME=gradapp
DB_USER=gradapp
BUCKET="${PROJECT}-grad-app-files"
SA="grad-app-run"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

gcloud config set project "$PROJECT" >/dev/null

say "Enabling the APIs this needs"
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com storage.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudtasks.googleapis.com

# --------------------------------------------------------------------------
# Postgres. db-f1-micro has no SLA and is meant for low traffic — right for a
# pilot, worth revisiting before a real admissions cycle depends on it.
# --------------------------------------------------------------------------
if ! gcloud sql instances describe "$DB_INSTANCE" >/dev/null 2>&1; then
  say "Creating Cloud SQL (a few minutes)"
  gcloud sql instances create "$DB_INSTANCE" \
    --database-version=POSTGRES_15 --tier=db-f1-micro --region="$REGION" \
    --storage-size=10GB --storage-auto-increase \
    --backup --backup-start-time=09:00
else
  say "Cloud SQL already exists — leaving it alone"
fi

gcloud sql databases describe "$DB_NAME" --instance="$DB_INSTANCE" >/dev/null 2>&1 || \
  gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE"

# --------------------------------------------------------------------------
# Secrets. Generated here and never printed: the database password and session
# key exist only in Secret Manager, and the deployer doesn't need to see either.
# --------------------------------------------------------------------------
ensure_secret() {   # name, generator
  if ! gcloud secrets describe "$1" >/dev/null 2>&1; then
    say "Creating secret $1"
    eval "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic
  fi
}

ensure_secret grad-app-db-password "python3 -c 'import secrets;print(secrets.token_urlsafe(32),end=\"\")'"
ensure_secret grad-app-secret-key  "python3 -c 'import secrets;print(secrets.token_hex(32),end=\"\")'"
ensure_secret grad-app-setup-token "python3 -c 'import secrets;print(secrets.token_urlsafe(16),end=\"\")'"

DB_PASSWORD="$(gcloud secrets versions access latest --secret=grad-app-db-password)"
gcloud sql users describe "$DB_USER" --instance="$DB_INSTANCE" >/dev/null 2>&1 \
  && gcloud sql users set-password "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD" \
  || gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD"

# The connection string contains the password, so it goes in Secret Manager
# rather than in --set-env-vars. Env vars are shown in plain text on the Cloud
# Run service page and in `gcloud run services describe`, which means anyone
# with Viewer on the project could read the database credentials for a system
# holding applicant records.
CONNECTION="${PROJECT}:${REGION}:${DB_INSTANCE}"
DSN="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${CONNECTION}"
if gcloud secrets describe grad-app-db-url >/dev/null 2>&1; then
  printf '%s' "$DSN" | gcloud secrets versions add grad-app-db-url --data-file=- >/dev/null
else
  printf '%s' "$DSN" | gcloud secrets create grad-app-db-url --data-file=- \
    --replication-policy=automatic
fi
unset DB_PASSWORD DSN

# --------------------------------------------------------------------------
# Bucket. Uniform access + public access prevention: this holds applicants'
# original PDFs, so the failure mode of a misconfigured ACL is somebody's
# transcript being world-readable.
# --------------------------------------------------------------------------
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  say "Creating the file bucket"
  gcloud storage buckets create "gs://$BUCKET" \
    --location="$REGION" --uniform-bucket-level-access \
    --public-access-prevention
fi

# --------------------------------------------------------------------------
# A service account with exactly what the app needs, rather than the default
# Compute account, which has edit on the whole project.
# --------------------------------------------------------------------------
SA_EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA" --display-name="Grad app (Cloud Run)"

for role in roles/cloudsql.client roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA_EMAIL" --role="$role" --condition=None >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_EMAIL" --role=roles/storage.objectAdmin >/dev/null

# --------------------------------------------------------------------------
# The ingest queue. Without this an upload is processed on the instance that
# received it, and a deploy or scale-down mid-ingest loses the rest of the
# batch. Cloud Tasks holds each file until a 2xx comes back.
# --------------------------------------------------------------------------
QUEUE_NAME=grad-app-ingest
if ! gcloud tasks queues describe "$QUEUE_NAME" --location="$REGION" >/dev/null 2>&1; then
  say "Creating the ingest queue"
  # Concurrency is capped low on purpose: every task is an OCR job competing
  # for the same small database, and letting fifty run at once would be slower
  # than eight, not faster.
  gcloud tasks queues create "$QUEUE_NAME" --location="$REGION" \
    --max-concurrent-dispatches=8 --max-attempts=5 \
    --min-backoff=10s --max-backoff=300s
fi
QUEUE_PATH="projects/${PROJECT}/locations/${REGION}/queues/${QUEUE_NAME}"

# Cloud Tasks signs its callback as this account, and /tasks/run verifies it —
# the endpoint writes to the database and has no session behind it.
TASKS_SA="grad-app-tasks"
TASKS_EMAIL="${TASKS_SA}@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$TASKS_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$TASKS_SA" --display-name="Grad app (Cloud Tasks caller)"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA_EMAIL" --role=roles/cloudtasks.enqueuer --condition=None >/dev/null
# The app's own account has to be able to mint tokens as the caller account.
gcloud iam service-accounts add-iam-policy-binding "$TASKS_EMAIL" \
  --member="serviceAccount:$SA_EMAIL" --role=roles/iam.serviceAccountTokenCreator >/dev/null

# --------------------------------------------------------------------------
# Build and deploy.
# --------------------------------------------------------------------------
say "Building the image"
gcloud builds submit --tag "gcr.io/${PROJECT}/${SERVICE}"

say "Deploying"
gcloud run deploy "$SERVICE" \
  --image "gcr.io/${PROJECT}/${SERVICE}" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$CONNECTION" \
  --set-env-vars "GRADAPP_ENV=cloud,GRADAPP_BUCKET=${BUCKET},GRADAPP_DATA=/tmp/gradapp-data" \
  --set-env-vars "GRADAPP_USERS_FILE=/tmp/gradapp-data/config/users.yaml" \
  --set-env-vars "GRADAPP_PROFESSORS_FILE=/tmp/gradapp-data/config/professors.yaml" \
  --set-secrets "GRADAPP_DATABASE_URL=grad-app-db-url:latest" \
  --set-secrets "GRADAPP_SECRET_KEY=grad-app-secret-key:latest,GRADAPP_SETUP_TOKEN=grad-app-setup-token:latest" \
  --memory 2Gi --cpu 2 --timeout 900 \
  --min-instances 0 --max-instances 4 \
  --allow-unauthenticated

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"

# Chicken and egg: a queued task calls the service back, so it needs the URL,
# which doesn't exist until the first deploy. Set it on a second pass. On every
# later run these are already correct and this is a no-op revision.
say "Pointing the queue at $URL"
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars "GRADAPP_TASK_QUEUE=${QUEUE_PATH},GRADAPP_BASE_URL=${URL},GRADAPP_TASK_INVOKER=${TASKS_EMAIL}" \
  >/dev/null

# Only the Cloud Tasks caller may reach /tasks/run. The service is otherwise
# public (it has its own login), so this is belt and braces on the one endpoint
# that runs ingest without a session.
gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" \
  --member="serviceAccount:$TASKS_EMAIL" --role=roles/run.invoker >/dev/null

TOKEN="$(gcloud secrets versions access latest --secret=grad-app-setup-token)"

cat <<EOF

Deployed: $URL

The app has no accounts yet, so it is serving nothing at all — every route
redirects to /setup, and /setup returns 404 without the claim token. Open this
once to create the advisor account:

    ${URL}/setup?token=${TOKEN}

Then, so the link can't be reused:

    gcloud run services update $SERVICE --region $REGION \\
      --remove-secrets GRADAPP_SETUP_TOKEN

Uploads are queued: each file becomes a Cloud Task, so a deploy or a
scale-down part-way through a batch no longer loses the rest of it — the queue
redelivers whatever didn't finish.

--allow-unauthenticated puts this on the public internet behind the app's own
login. If the applicant-data conversation hasn't landed yet, drop that flag and
use 'gcloud run services proxy' instead, which keeps it reachable only by you.
EOF
