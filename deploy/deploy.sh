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

# gcloud asks questions — most usefully "API [x] not enabled. Enable and
# retry? (y/N)". The existence probes below send stderr to /dev/null, so such a
# prompt is invisible and the script just appears to hang mid-list with no
# cursor and nothing to type at. Turn prompting off globally: this script's own
# confirmation uses `read`, and every gcloud call here should either work or
# fail, never ask.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

PROJECT="${1:?usage: deploy.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"

# Every resource is named from one prefix, so this can live alongside whatever
# else is in an existing project without colliding — and so a second copy (a
# staging one, say) is one variable away:
#
#   NAME=grad-app-test ./deploy/deploy.sh my-project
NAME="${NAME:-grad-app}"

# Checked here because every GCP resource type has its own name rules and the
# errors are unhelpful and arrive late — a space in NAME surfaces as a rejected
# bucket name several minutes and one Cloud SQL instance into the run.
if ! [[ "$NAME" =~ ^[a-z][a-z0-9-]{1,28}[a-z0-9]$ ]]; then
  printf '\n\033[1;31mNAME=%s is not a usable resource name.\033[0m\n' "$NAME" >&2
  printf 'Lowercase letters, digits and hyphens only; start with a letter.\n' >&2
  printf "Spaces aren't allowed anywhere in GCP — try %s\n\n" \
    "$(echo "$NAME" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-')" >&2
  exit 1
fi

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
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)' 2>/dev/null || true)"

say "Checking you can do what this needs in $PROJECT"
NEEDED=(
  resourcemanager.projects.setIamPolicy
  iam.serviceAccounts.create
  cloudsql.instances.create
  storage.buckets.create
  secretmanager.secrets.create
  secretmanager.versions.add
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
# Timeboxed: a probe against a disabled API or an unreachable service should
# report "CREATE" and move on, not stall the listing. Anything that can't be
# read in five seconds is treated as absent, which is the safe reading — the
# create steps below are all guarded and will reuse whatever is really there.
exists() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 bash -c "$1" >/dev/null 2>&1 && echo "reuse " || echo "CREATE"
  else
    eval "$1" >/dev/null 2>&1 && echo "reuse " || echo "CREATE"
  fi
}
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
# Two rules here, both learned the hard way against a locked-down project.
#
# 1. --replication-policy=user-managed pinned to $REGION, not "automatic".
#    Automatic replication puts the secret in "global", which an organization
#    policy of constraints/gcp.resourceLocations rejects outright.
#
# 2. **This script never reads a secret back.** Creating secrets and reading
#    their values are separate permissions, and an account can easily have the
#    first without the second — writing config is an admin job, reading
#    production credentials generally isn't. So every value is generated here,
#    used from a shell variable, and written outward only. The app itself reads
#    them at runtime through --set-secrets, using its own service account,
#    which is the only identity that actually needs access.

put_secret() {   # name, value — create it, or add a new version
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    say "Creating secret $1"
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- \
      --replication-policy=user-managed --locations="$REGION" >/dev/null
  fi
}

gen() { python3 -c "import secrets;print(secrets.$1,end='')"; }

# The session key must survive redeploys — rotating it signs everyone out — and
# since we can't read the existing one, it is written exactly once and left
# alone thereafter. Nothing here ever needs its value.
if ! gcloud secrets describe "${NAME}-secret-key" >/dev/null 2>&1; then
  put_secret "${NAME}-secret-key" "$(gen 'token_hex(32)')"
fi

# The database password is rotated on every deploy instead of being read back.
# The user's password and the connection string are set from the same variable
# in the same run, so they can't drift; nothing else holds a copy.
DB_PASSWORD="$(gen 'token_urlsafe(32)')"
put_secret "${NAME}-db-password" "$DB_PASSWORD"    # a place a human can find it

gcloud sql users describe "$DB_USER" --instance="$DB_INSTANCE" >/dev/null 2>&1 \
  && gcloud sql users set-password "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD" \
  || gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD"

# The connection string contains the password, so it goes in Secret Manager
# rather than in --set-env-vars. Env vars are shown in plain text on the Cloud
# Run service page and in `gcloud run services describe`, which means anyone
# with Viewer on the project could read the database credentials for a system
# holding applicant records.
CONNECTION="${PROJECT}:${REGION}:${DB_INSTANCE}"
put_secret "${NAME}-db-url" \
  "postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${CONNECTION}"
unset DB_PASSWORD

# Rotated on every deploy rather than created once. It only matters while the
# app is unclaimed — /setup closes itself the moment an advisor account exists —
# so a fresh one costs nothing, invalidates any older copy, and is always
# printable without needing to read it back.
SETUP_TOKEN="$(gen 'token_urlsafe(16)')"
put_secret "${NAME}-setup-token" "$SETUP_TOKEN"

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
# SERVICE_ACCOUNT lets you point at an account that already holds the roles,
# for when you can create resources in a project but not grant roles on it.
SA_EMAIL="${SERVICE_ACCOUNT:-${SA}@${PROJECT}.iam.gserviceaccount.com}"
if [ -z "${SERVICE_ACCOUNT:-}" ]; then
  gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$SA" --display-name="Grad app (Cloud Run)"
fi

# Granting a role on the *project* needs resourcemanager.projects.setIamPolicy,
# which departmental projects rarely hand out — and shouldn't have to, since
# most of what this app needs can be granted on the individual resources
# instead. So: try the narrow grant first, fall back to the project, and record
# anything that neither works for rather than dying on it. What's left is
# printed at the end as an exact request for whoever does administer the
# project, which is far more useful than a stack trace.
NEEDS_ADMIN=()

# Naming an existing account means "this one already has what it needs", so
# every grant below is skipped rather than attempted and failed. Without this
# the escape hatch hits exactly the wall it exists to get around.
BYO_SA="${SERVICE_ACCOUNT:-}"

grant_project() {   # role
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA_EMAIL" --role="$1" --condition=None >/dev/null 2>&1 \
    || NEEDS_ADMIN+=("gcloud projects add-iam-policy-binding $PROJECT \\
    --member=serviceAccount:$SA_EMAIL \\
    --role=$1 --condition=None")
}

# Bucket and secrets take resource-level bindings, and you can set those on
# things you created — no project-wide rights involved.
if [ -z "$BYO_SA" ]; then
  gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member="serviceAccount:$SA_EMAIL" --role=roles/storage.objectAdmin >/dev/null 2>&1 \
    || grant_project roles/storage.objectAdmin
fi

for secret in $([ -z "$BYO_SA" ] && echo "${NAME}-db-url ${NAME}-secret-key ${NAME}-setup-token"); do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:$SA_EMAIL" --role=roles/secretmanager.secretAccessor \
    >/dev/null 2>&1 || NEEDS_ADMIN+=("gcloud secrets add-iam-policy-binding $secret \\
    --member=serviceAccount:$SA_EMAIL \\
    --role=roles/secretmanager.secretAccessor")
done

# Cloud SQL has no per-instance IAM, so this one is project-level or nothing.
[ -z "$BYO_SA" ] && grant_project roles/cloudsql.client || true

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
# The queue is a durability upgrade, not a requirement: without it the app
# ingests on a request thread, exactly as it did before the queue existed. So
# these two grants degrade rather than block — a pilot that works today beats a
# perfect one that waits on a ticket.
QUEUE_READY=1
if [ -n "$BYO_SA" ]; then
  : # a supplied account is assumed to hold these already
else
  gcloud tasks queues add-iam-policy-binding "$QUEUE_NAME" --location="$REGION" \
    --member="serviceAccount:$SA_EMAIL" --role=roles/cloudtasks.enqueuer \
    >/dev/null 2>&1 || QUEUE_READY=0
  gcloud iam service-accounts add-iam-policy-binding "$TASKS_EMAIL" \
    --member="serviceAccount:$SA_EMAIL" --role=roles/iam.serviceAccountTokenCreator \
    >/dev/null 2>&1 || QUEUE_READY=0
fi
if [ "$QUEUE_READY" = 0 ]; then
  QUEUE_NOTE="Queued ingest is OFF — the grants for it were refused. Uploads are
processed on the request thread, which is how this worked before the queue
existed. If a deploy or scale-down lands mid-upload the rest of that batch is
lost and has to be re-uploaded; nothing already read is lost. To turn it on:

  gcloud tasks queues add-iam-policy-binding $QUEUE_NAME --location=$REGION \\
    --member=serviceAccount:$SA_EMAIL --role=roles/cloudtasks.enqueuer
  gcloud iam service-accounts add-iam-policy-binding $TASKS_EMAIL \\
    --member=serviceAccount:$SA_EMAIL --role=roles/iam.serviceAccountTokenCreator

then re-run this script."
else
  QUEUE_NOTE=""
fi

# Stop here rather than deploying something that cannot start. Without
# cloudsql.client the container comes up, fails to reach the database, and
# crash-loops -- which looks like a bug in the app rather than a missing grant.
if [ ${#NEEDS_ADMIN[@]} -gt 0 ]; then
  printf '\n\033[1;31m%s\033[0m\n' "Everything is built except the permissions below."
  cat <<EOF

Your account can create resources in this project but not grant roles on it
(resourcemanager.projects.setIamPolicy). These are what the app's service
account still needs. Send them to whoever administers
$PROJECT -- they are copy-pasteable as-is:

EOF
  printf '  %s\n\n' "${NEEDS_ADMIN[@]}"
  cat <<EOF
Nothing is wasted: Cloud SQL, the bucket, the secrets, the queue and the
service accounts all exist. Re-run this script once the grants are in and it
will carry on from here.

If you would rather not wait, you can deploy using an existing service account
that already holds these roles -- often the default compute one:

  SERVICE_ACCOUNT=${PROJECT_NUMBER:-PROJECT_NUMBER}-compute@developer.gserviceaccount.com \\
    ./deploy/deploy.sh $PROJECT

That trades least-privilege for speed: the default account usually has Editor
on the whole project, so the app would be able to reach far more than it needs.
Fine for a pilot on synthetic data, worth undoing before real applicants.
EOF
  exit 1
fi

# --------------------------------------------------------------------------
# Build and deploy.
# --------------------------------------------------------------------------
# gcr.io is multi-regional storage in the US, which the same location policy
# can refuse. A regional Artifact Registry repo keeps the image where the rest
# of this lives — and gcr.io is the legacy path anyway.
REPO="${NAME}-images"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}"
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format=docker \
    --location="$REGION" --description="Grad app container images"

say "Building the image"
gcloud builds submit --tag "$IMAGE" --region="$REGION"

say "Deploying"
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
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
# The invoker binding comes BEFORE the queue is switched on, and gates it.
# A queued task calls the service back over HTTP; if the tasks account can't
# invoke the service, every callback 403s and uploads enqueue and then silently
# never complete — worse than not queueing at all, because it looks like it
# should work. Ordering these the other way round shipped exactly that.
if [ "$QUEUE_READY" = 1 ]; then
  if gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" \
       --member="serviceAccount:$TASKS_EMAIL" --role=roles/run.invoker >/dev/null 2>&1; then
    say "Pointing the queue at $URL"
    gcloud run services update "$SERVICE" --region "$REGION" \
      --update-env-vars "GRADAPP_TASK_QUEUE=${QUEUE_PATH},GRADAPP_BASE_URL=${URL},GRADAPP_TASK_INVOKER=${TASKS_EMAIL}" \
      >/dev/null
  else
    QUEUE_READY=0
    QUEUE_NOTE="Queued ingest is OFF. The deploy could not grant Cloud Tasks
permission to call this service back (roles/run.invoker), so every queued task
would have been rejected and uploads would have hung. Ingest runs on the
request thread instead, which is how this worked before the queue existed.

To turn it on, an administrator runs:

  gcloud run services add-iam-policy-binding $SERVICE --region $REGION \\
    --member=serviceAccount:$TASKS_EMAIL --role=roles/run.invoker

then re-run this script."
    # and make sure a previous revision's queue settings don't linger
    gcloud run services update "$SERVICE" --region "$REGION" \
      --remove-env-vars GRADAPP_TASK_QUEUE,GRADAPP_BASE_URL,GRADAPP_TASK_INVOKER \
      >/dev/null 2>&1 || true
  fi
fi

# Whether the service is reachable at all. --allow-unauthenticated is itself an
# IAM policy change, so on a locked-down project it fails and leaves a service
# nobody can open — with the failure buried in a "Completed with warnings" line
# that is easy to read past.
PUBLIC="$(gcloud run services get-iam-policy "$SERVICE" --region "$REGION" \
  --format='value(bindings.members)' 2>/dev/null | grep -c allUsers || true)"

cat <<EOF

Deployed: $URL

$(if [ "${PUBLIC:-0}" = 0 ]; then cat <<INNER
NOT REACHABLE YET. Granting public access is an IAM change, and this project
refused it, so opening that URL in a browser returns 403. Two ways on:

  Right now, just you:
    gcloud run services proxy $SERVICE --region $REGION
    then open http://localhost:8080

  For everyone (an administrator runs this once):
    gcloud run services add-iam-policy-binding $SERVICE --region $REGION \\
      --member=allUsers --role=roles/run.invoker

Public here means "reachable"; the app's own login still gates every page.
INNER
fi)

If this is the first deploy, the app has no accounts and is serving nothing at
all — every route redirects to /setup, and /setup returns 404 without the claim
token. Open this once to create the advisor account:

    ${URL}/setup?token=${SETUP_TOKEN}

(That token is rotated on every deploy, so an older link no longer works.)

Once the advisor account exists, remove it so the link can't be reused:

    gcloud run services update $SERVICE --region $REGION \\
      --remove-secrets GRADAPP_SETUP_TOKEN

${QUEUE_NOTE:-Uploads are queued: each file becomes a Cloud Task, so a deploy or a
scale-down part-way through a batch no longer loses the rest of it — the queue
redelivers whatever did not finish.}

--allow-unauthenticated puts this on the public internet behind the app's own
login. If the applicant-data conversation hasn't landed yet, drop that flag and
use 'gcloud run services proxy' instead, which keeps it reachable only by you.
EOF
