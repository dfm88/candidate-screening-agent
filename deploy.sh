#!/usr/bin/env bash
# Deploys the app to Cloud Run + Cloud SQL. Re-runnable: every resource is created only if missing.
# Configuration comes from the environment or a gitignored deploy.env (see deploy.env.example).
set -euo pipefail
cd "$(dirname "$0")"

# deploy.env fills in only what the environment doesn't already set, so an explicit env var always wins. The `||`
# keeps a last line without a newline, which `read` returns as a failure.
if [[ -f deploy.env ]]; then
  while IFS='=' read -r key value || [[ -n "$key" ]]; do
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
# Joined with ";", not ",": SPEECH_ENGINES is JSON (gcloud's ^;^ delimiter, see `gcloud topic escaping`).
env_vars="ENVIRONMENT=${ENVIRONMENT:-demo};GOOGLE_CLOUD_PROJECT=${GCP_PROJECT};LLM_CONTENT_LOGGING=${LLM_CONTENT_LOGGING}"
if [[ -n "${LOGFIRE_TOKEN:-}" ]]; then
  log "LOGFIRE_TOKEN secret"
  upsert_secret logfire-token "$LOGFIRE_TOKEN"
fi
if gc secrets describe logfire-token >/dev/null 2>&1; then
  secrets="${secrets},LOGFIRE_TOKEN=logfire-token:latest"
fi
# The public apply form asks for this code (the reviewer link carries it); without it anyone with the URL can apply.
if [[ -n "${APPLY_ACCESS_CODE:-}" ]]; then
  log "APPLY_ACCESS_CODE secret"
  upsert_secret apply-access-code "$APPLY_ACCESS_CODE"
fi
if gc secrets describe apply-access-code >/dev/null 2>&1; then
  secrets="${secrets},APPLY_ACCESS_CODE=apply-access-code:latest"
fi
# The recruiter tool's shared password; without it the recruiter tool stays closed.
if [[ -n "${RECRUITER_PASSWORD:-}" ]]; then
  log "RECRUITER_PASSWORD secret"
  upsert_secret recruiter-password "$RECRUITER_PASSWORD"
fi
if gc secrets describe recruiter-password >/dev/null 2>&1; then
  secrets="${secrets},RECRUITER_PASSWORD=recruiter-password:latest"
fi
# Signs the recruiter's session cookie: SESSION_SECRET when given (to rotate it), otherwise generated once and kept, so
# sessions survive restarts and redeploys.
if [[ -n "${SESSION_SECRET:-}" ]]; then
  log "SESSION_SECRET secret"
  upsert_secret session-secret "$SESSION_SECRET"
elif ! gc secrets describe session-secret >/dev/null 2>&1; then
  log "SESSION_SECRET secret (generated)"
  upsert_secret session-secret "$(openssl rand -hex 32)"
fi
secrets="${secrets},SESSION_SECRET=session-secret:latest"
# The secret in the URL of the recruiters' MCP server (/mcp/<token>), its only auth: MCP_TOKEN when given (to rotate
# it), otherwise generated once and kept, so a connector keeps working across redeploys. Never printed: the README
# copies the URL to the clipboard.
if [[ -n "${MCP_TOKEN:-}" ]]; then
  log "MCP_TOKEN secret"
  upsert_secret mcp-token "$MCP_TOKEN"
elif ! gc secrets describe mcp-token >/dev/null 2>&1; then
  log "MCP_TOKEN secret (generated)"
  upsert_secret mcp-token "$(openssl rand -hex 32)"
fi
secrets="${secrets},MCP_TOKEN=mcp-token:latest"
# The screening model (OpenAI) can't run without its key.
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  log "OPENAI_API_KEY secret"
  upsert_secret openai-api-key "$OPENAI_API_KEY"
fi
if ! gc secrets describe openai-api-key >/dev/null 2>&1; then
  echo "Set OPENAI_API_KEY in deploy.env: the screening model needs it." >&2
  exit 1
fi
secrets="${secrets},OPENAI_API_KEY=openai-api-key:latest"
# The fallback model (the Gemini API) answers only when OpenAI fails; without its key the app has no fallback.
if [[ -n "${GOOGLE_API_KEY:-}" ]]; then
  log "GOOGLE_API_KEY secret"
  upsert_secret google-api-key "$GOOGLE_API_KEY"
fi
if gc secrets describe google-api-key >/dev/null 2>&1; then
  secrets="${secrets},GOOGLE_API_KEY=google-api-key:latest"
else
  env_vars="${env_vars};SCREENING_FALLBACK_MODELS=[]"
fi
# Live voice calls (ElevenLabs Speech Engine). Without the key the chat has no call button. The engines' ids come from
# `ENVIRONMENT=demo python -m screening.voice wss://SERVICE_URL/api/voice/ws`, run once the service has its URL;
# without them the call button stays hidden too.
if [[ -n "${ELEVENLABS_API_KEY:-}" ]]; then
  log "ELEVENLABS_API_KEY secret"
  upsert_secret elevenlabs-api-key "$ELEVENLABS_API_KEY"
fi
if gc secrets describe elevenlabs-api-key >/dev/null 2>&1; then
  secrets="${secrets},ELEVENLABS_API_KEY=elevenlabs-api-key:latest"
fi
if [[ -n "${SPEECH_ENGINES:-}" ]]; then
  env_vars="${env_vars};SPEECH_ENGINES=${SPEECH_ENGINES}"
fi
# The demo prints the ElevenLabs SDK's log of each call (event ids, not the transcripts) to Cloud Logging, to
# trace a call that ElevenLabs ends on its side.
env_vars="${env_vars};VOICE_DEBUG=${VOICE_DEBUG:-true}"

log "Deploying ${SERVICE} to Cloud Run"
# Exactly one always-on instance with CPU always allocated: the in-process reminder loop, SSE streams and
# per-conversation locks all live in that process (scaling out is described in the README).
gc run deploy "$SERVICE" --source . --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$connection_name" \
  --set-secrets "$secrets" \
  --set-env-vars "^;^${env_vars}" \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --cpu 1 --memory 1Gi --timeout 3600 \
  --allow-unauthenticated

url="$(gc run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
log "Deployed: ${url}"
curl -fsS "${url}/api/health" && echo
