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

# Every resource is named from one prefix, so this can live alongside whatever
# else is in an existing project without colliding — and so a second copy (a
# staging one, say) is one variable away:
#
#   NAME=grad-app-test ./deploy/deploy.sh my-project
NAME="${NAME:-grad-app}"

SERVICE="$NAME"
DB_INSTANCE="${NAME}-db"
DB_NAME=gradapp
DB_USER=gradapp
BUCKET="${PROJECT}-${NAME}-files"
SA="${NAME}-run"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
die() { printf '\n\033[1;31m%s\033[0m\n' "$1" >&2; exit 1; }

# `set -e` makes any unhandled failure exit silently, which in a 200-line deploy
# script means staring at a prompt with no idea what happened. Report where.
trap 'printf "\n\033[1;31mdeploy.sh failed at line %s: %s\033[0m\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

# --------------------------------------------------------------------------
# Preflight.
#
# This is written to be run against a project you may not own — a shared
# departmental one, say — where a script that dies halfway through leaves
# service accounts and a Cloud SQL instance behind for somebody else to find
# and wonder about. So: check access, check permissions, show what will be
# created versus reused, and get a yes before touching anything.
# --------------------------------------------------------------------------
gcloud projects describe "$PROJECT" >/dev/null 2>&1 || die \
"Can't see project '$PROJECT' as $(gcloud config get-value account 2>/dev/null).

Projects you can access:
$(gcloud projects list --format='  value(projectId)' 2>/dev/null || echo '  (none — gcloud auth login?)')

Pass one of those:  ./deploy/deploy.sh PROJECT_ID"

gcloud config set project "$PROJECT" >/dev/null

say "Checking you can do what this needs in $PROJECT"
NEEDED=(
  resourcemanager.projects.setIamPolicy
  iam.serviceAccounts.create
  cloudsql.instances.create
  storage.buckets.create
  secretmanager.secrets.create
  run.services.create
  cloudtasks.queues.create
)
# `|| true`: this probe is advisory, and it fails in its own right on a locked
# down project — testing your permissions itself needs a permission. Its failure
# must not take the script down, which is exactly what it did the first time.
GRANTED="$(gcloud projects test-iam-permissions "$PROJECT" \
  --permissions="$(IFS=,; echo "${NEEDED[*]}")" \
  --format='value(permissions)' 2>/dev/null | tr ';,' '\n\n' || true)"

if [ -z "$GRANTED" ]; then
  cat <<EOF

  Couldn't read your permissions on this project — either the probe itself is
  denied, or the Resource Manager API is off. That's common on a managed
  departmental project and doesn't necessarily mean the deploy will fail.

  Continuing will find out for real. The first thing to break would be
  creating the Cloud SQL instance, and nothing is billable before that.

EOF
else
  MISSING=()
  for perm in "${NEEDED[@]}"; do
    grep -qx "$perm" <<<"$GRANTED" || MISSING+=("$perm")
  done
  if [ ${#MISSING[@]} -gt 0 ]; then
    die "Your account is missing these permissions on '$PROJECT':

$(printf '  %s\n' "${MISSING[@]}")

Forward exactly that list to whoever administers the project — it's more
actionable than 'it didn't work'. roles/editor plus
roles/resourcemanager.projectIamAdmin covers all of them.

Stopping now rather than part-way through."
  fi
  say "Permissions look fine"
fi

# What already exists, so nothing is created by surprise in a shared project.
say "What this will do in $PROJECT"
exists() { eval "$1" >/dev/null 2>&1 && echo "reuse " || echo "CREATE"; }
printf '  %s  Cloud SQL instance  %s\n' \
  "$(exists "gcloud sql instances describe $DB_INSTANCE")" "$DB_INSTANCE"
printf '  %s  Storage bucket      %s\n' \
  "$(exists "gcloud storage buckets describe gs://$BUCKET")" "$BUCKET"
printf '  %s  Cloud Tasks queue   %s\n' \
  "$(exists "gcloud tasks queues describe ${NAME}-ingest --location=$REGION")" "${NAME}-ingest"
printf '  %s  Cloud Run service   %s\n' \
  "$(exists "gcloud run services describe $SERVICE --region=$REGION")" "$SERVICE"
printf '\n  Region: %s\n  Ongoing cost: about $10/month, almost all Cloud SQL.\n' "$REGION"
printf '  Nothing else in this project is touched.\n\n'

read -r -p "Continue? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || die "Stopped. Nothing was created."

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

ensure_secret ${NAME}-db-password "python3 -c 'import secrets;print(secrets.token_urlsafe(32),end=\"\")'"
ensure_secret ${NAME}-secret-key  "python3 -c 'import secrets;print(secrets.token_hex(32),end=\"\")'"
ensure_secret ${NAME}-setup-token "python3 -c 'import secrets;print(secrets.token_urlsafe(16),end=\"\")'"

DB_PASSWORD="$(gcloud secrets versions access latest --secret=${NAME}-db-password)"
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
if gcloud secrets describe ${NAME}-db-url >/dev/null 2>&1; then
  printf '%s' "$DSN" | gcloud secrets versions add ${NAME}-db-url --data-file=- >/dev/null
else
  printf '%s' "$DSN" | gcloud secrets create ${NAME}-db-url --data-file=- \
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
QUEUE_NAME="${NAME}-ingest"
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
TASKS_SA="${NAME}-tasks"
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
  `# One flag each, comma-separated. Repeating --set-env-vars does NOT merge:` \
  `# gcloud takes the last occurrence and silently drops the rest, which would` \
  `# leave GRADAPP_ENV unset and boot the container in local mode -- SQLite, no` \
  `# bucket, and none of the cloud guardrails, with nothing to indicate it.` \
  --set-env-vars "GRADAPP_ENV=cloud,GRADAPP_BUCKET=${BUCKET},GRADAPP_DATA=/tmp/gradapp-data,GRADAPP_USERS_FILE=/tmp/gradapp-data/config/users.yaml,GRADAPP_PROFESSORS_FILE=/tmp/gradapp-data/config/professors.yaml" \
  --set-secrets "GRADAPP_DATABASE_URL=${NAME}-db-url:latest,GRADAPP_SECRET_KEY=${NAME}-secret-key:latest,GRADAPP_SETUP_TOKEN=${NAME}-setup-token:latest" \
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

TOKEN="$(gcloud secrets versions access latest --secret=${NAME}-setup-token)"

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
