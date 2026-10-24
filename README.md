# candidate-screening-agent

AI screening agent for delivery-driver applicants (fictional client: Grupo Sazón, Spain and Mexico). Candidates chat with a messaging-style assistant; recruiters track outcomes in a recruiter tool.

- Process design (Phase 1): [`docs/process-design.md`](docs/process-design.md)
- Agent and contributor rules: [`AGENTS.md`](AGENTS.md)

> Work in progress. Architecture, design decisions and improvements are added as the build progresses.

## Local development

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Node 22.

```bash
docker compose up -d --wait db            # Postgres 18 on localhost:55432 (override with DB_HOST_PORT)
cd backend
cp example.env .env                       # fill in what you need; every variable is documented there
uv sync
uv run --env-file .env alembic upgrade head
uv run --env-file .env uvicorn screening.main:create_app --factory --reload --port 8000

cd ../frontend
npm install
npm run dev                               # http://localhost:5173, proxies /api to :8000
```

**Configuration is environment-only.** `uv run --env-file .env` loads `backend/.env` into the process environment. The app settings, the Logfire SDK (`LOGFIRE_TOKEN`) and Google auth (`CLOUDSDK_CONFIG`) all read it from there, so there's no second loading mechanism to keep in sync. Logfire and GCP are optional: without a token, traces stay on the console. The frontend has no env file, because anything Vite exposes ends up in the public bundle.

**Chat with the agent in the terminal** (in memory, no database):

```bash
cd backend
uv run --env-file .env python -m screening.cli --vacancy mx-cdmx-roma --name "José Hernández" --show-tools
```

By default the models run on Vertex AI in the EU, which needs the Google Cloud variables in `.env`. To try it with a single API key instead, set for example `SCREENING_MODELS=google:gemini-3.8-flash` and `GOOGLE_API_KEY`. That path is for local testing with test data only: in production, candidate data goes only to Vertex AI in the EU.

To run the production image locally: `docker compose --profile app up --build` (http://localhost:8080). It picks up `backend/.env` if present.

## Deployment (GCP)

A single Cloud Run service runs the API and the built frontend, with Cloud SQL (Postgres 18), Secret Manager, and Vertex AI in the `eu` multi-region.

```bash
cp deploy.env.example deploy.env   # set GCP_PROJECT (and CLOUDSDK_CONFIG if you use a dedicated gcloud config)
./deploy.sh
```

`deploy.sh` is re-runnable. It refuses to run if the active gcloud project differs from `GCP_PROJECT`. It enables the required APIs and creates the service account, the Cloud SQL instance, the database and its secret only if they're missing, then deploys from source with Cloud Build.

The demo deployment logs full LLM prompts and completions to Logfire (`LLM_CONTENT_LOGGING=true`, synthetic data only). For a real client it stays off unless there's a DPA and a legal basis.

The service runs as exactly one always-on instance (`--min-instances 1 --max-instances 1 --no-cpu-throttling`), because the reminder loop, the SSE streams and the per-conversation locks live in that process.

**Budget alert.** Run this once, as protection against a runaway loop. The amount must be in the billing account's currency:

```bash
gcloud billing budgets create --billing-account=BILLING_ACCOUNT_ID \
  --display-name="candidate-screening-agent" --budget-amount=50EUR \
  --filter-projects=projects/$GCP_PROJECT \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```
