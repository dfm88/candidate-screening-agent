#!/usr/bin/env bash
# Deploys the app to Cloud Run + Cloud SQL. Re-runnable: every resource is created only if missing.
# Configuration comes from the environment or a gitignored deploy.env (see deploy.env.example).
set -euo pipefail
cd "$(dirname "$0")"

# deploy.env fills in only what the environment doesn't already set, so an explicit env var always wins.
if [[ -f deploy.env ]]; then
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    [[ -z "${!key:-}" ]] && export "$key=$value"
  done < deploy.env
fi

: "${GCP_PROJECT:?Set GCP_PROJECT in the environment or in deploy.env}"
REGION="${REGION:-europe-southwest1}"
SERVICE="${SERVICE:-candidate-screening-agent}"
SQL_INSTANCE="${SQL_INSTANCE:-screening-pg}"
SQL_TIER="${SQL_TIER:-db-custom-1-3840}"
DB_NAME="screening"
DB_USER="screening"
SA_NAME="screening-run"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
# This is a demo deployment with synthetic data, so full LLM traces are on. For a real client it stays off
# unless there is a DPA and a legal basis for sending candidate conversations to the tracing backend.
LLM_CONTENT_LOGGING="${LLM_CONTENT_LOGGING:-true}"

# Guard against deploying with the wrong gcloud account/config (e.g. a work default).
active_project="$(gcloud config get-value project 2>/dev/null || true)"
if [[ "$active_project" != "$GCP_PROJECT" ]]; then
  echo "Refusing to deploy: active gcloud project is '${active_project}', expected '${GCP_PROJECT}'." >&2
  echo "Set CLOUDSDK_CONFIG to the right gcloud config directory (deploy.env) and retry." >&2
  exit 1
fi
gc() { gcloud --project "$GCP_PROJECT" --quiet "$@"; }
log() { printf '\n==> %s\n' "$*"; }

log "Enabling APIs"
gc services enable \
  run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com aiplatform.googleapis.com \
  cloudquotas.googleapis.com billingbudgets.googleapis.com

log "Service account ${SA_EMAIL}"
if ! gc iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  gc iam service-accounts create "$SA_NAME" --display-name "Candidate screening agent (Cloud Run)"
fi
for role in roles/cloudsql.client roles/aiplatform.user roles/secretmanager.secretAccessor; do
  gc projects add-iam-policy-binding "$GCP_PROJECT" --member "serviceAccount:${SA_EMAIL}" \
    --role "$role" --condition None >/dev/null
done

log "Cloud SQL instance ${SQL_INSTANCE} (Postgres 18, ${REGION})"
if ! gc sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  # Postgres 16+ defaults to Enterprise Plus; Enterprise is plenty for one small instance.
  gc sql instances create "$SQL_INSTANCE" --database-version POSTGRES_18 --edition enterprise \
    --tier "$SQL_TIER" --region "$REGION" --storage-auto-increase
fi
if ! gc sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1; then
  gc sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"
fi
connection_name="$(gc sql instances describe "$SQL_INSTANCE" --format 'value(connectionName)')"

upsert_secret() {
  local name="$1" value="$2"
  if gc secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gc secrets versions add "$name" --data-file - >/dev/null
  else
    printf '%s' "$value" | gc secrets create "$name" --replication-policy automatic --data-file - >/dev/null
  fi
}

log "Database user and DATABASE_URL secret"
if ! gc secrets describe database-url >/dev/null 2>&1; then
  # Hex only, so the password needs no URL escaping.
  db_password="$(openssl rand -hex 24)"
  if gc sql users list --instance "$SQL_INSTANCE" --format 'value(name)' | grep -qx "$DB_USER"; then
    gc sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$db_password"
  else
    gc sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$db_password"
  fi
  upsert_secret database-url \
    "postgresql+asyncpg://${DB_USER}:${db_password}@/${DB_NAME}?host=/cloudsql/${connection_name}"
fi

secrets="DATABASE_URL=database-url:latest"
if [[ -n "${LOGFIRE_TOKEN:-}" ]]; then
  log "LOGFIRE_TOKEN secret"
  upsert_secret logfire-token "$LOGFIRE_TOKEN"
fi
if gc secrets describe logfire-token >/dev/null 2>&1; then
  secrets="${secrets},LOGFIRE_TOKEN=logfire-token:latest"
fi

log "Deploying ${SERVICE} to Cloud Run"
# Exactly one always-on instance with CPU always allocated: the in-process reminder loop, SSE streams and
# per-conversation locks all live in that process (scaling out is described in the README).
gc run deploy "$SERVICE" --source . --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$connection_name" \
  --set-secrets "$secrets" \
  --set-env-vars "ENVIRONMENT=${ENVIRONMENT:-demo},GOOGLE_CLOUD_PROJECT=${GCP_PROJECT},LLM_CONTENT_LOGGING=${LLM_CONTENT_LOGGING}" \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --cpu 1 --memory 1Gi --timeout 3600 \
  --allow-unauthenticated

url="$(gc run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
log "Deployed: ${url}"
curl -fsS "${url}/api/health" && echo
