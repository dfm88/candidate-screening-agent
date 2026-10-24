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

**Models and where candidate data goes.** Live turns run on Gemini through Vertex AI in the `eu` multi-region, so processing stays in the EU. If that model fails (an outage, or a run of rate limits), PydanticAI's `FallbackModel` hands the message to the last fallback, an OpenAI model through OpenAI's API, which is outside the EU. It keeps a live chat answering, and it only sees data when the primary fails. Whether that's acceptable is a client decision (open question 4 in the process design); the EU-only alternatives are a second EU model as the fallback, or Claude in the EU once the project has quota. Both models are set in `.env` (`SCREENING_MODEL`, `SCREENING_FALLBACK_MODELS`).

To run without our Google Cloud project, set `SCREENING_MODEL=google:gemini-3.8-flash`, `SCREENING_FALLBACK_MODELS=[]` and `GOOGLE_API_KEY`. That's the same model through the Gemini API. This path is for local testing with test data only.

To run the production image locally: `docker compose --profile app up --build` (http://localhost:8080). It picks up `backend/.env` if present.

## Evals

The process design is enforced by evals, not unit tests. `backend/evals/` (Pydantic Evals) runs synthetic candidates from `cases.yaml`, in both countries and in Spanish and English, against the real agent. A simulated candidate (an LLM playing the persona) chats with the agent in memory until the conversation ends, and labels what each agent message asked for. Then:

- **Deterministic checks** carry the process rules: the outcome and review reasons, the saved answers, knockouts asked first, nothing asked again, at most two tries per field, message shape (at most 3 messages of 40 words, one question, last), no acknowledgement sent as a message on its own, reply language, the country's regional terms and gender-neutral wording. They decide the ranking.
- **An LLM judge** (OpenAI `gpt-6-luna`) scores tone, naturalness and language quality. Its scores on OpenAI rows are marked: judges tend to favour their own family's style.
- **Per config:** p50/p95 latency per turn, cost per completed screening (genai-prices), the share of input tokens read from the provider's prompt cache, cache collapses flagged by PydanticAI's instrumentation, and which model answered each turn.

```bash
cd backend
uv run --group evals --env-file .env python -m evals.run --model google-cloud:gemini-3.8-flash            # one model
uv run --group evals --env-file .env python -m evals.run --model google-cloud:gemini-3.8-flash \
    --fallback openai:gpt-6-luna                                                                       # the demo config
uv run --group evals --env-file .env python -m evals.run --model A --model B --repeat 2                # the arena
```

`--settings '{"temperature": 0.2}'` merges into the model settings. Rate limits are retried with backoff (Pydantic Evals' `retry_task`), never handed to another model, so a row measures one config. Results are appended to [`backend/evals/results/arena.md`](backend/evals/results/arena.md); full transcripts land in `backend/evals/results/transcripts/` (not committed). The simulator and the judge only see synthetic data, so they may run outside the EU.

### Why Gemini 3.8 Flash

The arena on the final prompt (9 cases × 2 runs per model, 2026-10-24; full table in [`arena.md`](backend/evals/results/arena.md)):

| | Gemini 3.8 Flash | Gemini 3.5 Flash | Gemini 3.5 Flash-Lite | gpt-6-luna |
|---|---|---|---|---|
| Runs in the EU | yes | yes | yes | no |
| All process checks | 0.97 | 0.94 | 0.97 | 0.99 |
| Outcome right | 1.00 | 1.00 | **0.94** | 1.00 |
| Nothing asked again | 1.00 | **0.67** | 0.94 | 0.94 |
| At most two tries | 1.00 | 1.00 | **0.89** | 1.00 |
| No acknowledgement on its own | 0.72 | 0.83 | 0.94 | 1.00 |
| Judge: tone / naturalness / language | 0.86 / 0.83 / 0.93 | 0.84 / 0.80 / 0.85 | 0.77 / 0.81 / 0.90 | 0.83 / 0.80 / 0.90 * |
| p50 / p95 per turn | 3.2 s / 10.7 s | 1.9 s / 2.5 s | 1.2 s / 1.9 s | 3.2 s / 6.1 s |
| Cost per completed screening | $0.047 | $0.045 | $0.010 | $0.001 |
| Input read from the prompt cache | 2% | 61% | 55% | 97% |

\* judged by a model of the same family.

- **Gemini 3.8 Flash is the only EU model with no process or outcome errors.** Its misses are style only (a closing "Muchas gracias" sent as its own message), and it has the best judge scores.
- **3.5 Flash-Lite** is 3× faster and 4.6× cheaper, but once turned "I don't know if my Colombian license is valid in Mexico" into a "no" (a wrongful knockout) and asked for an area three times. **3.5 Flash** saves the name from the application before the candidate confirms it.
- **gpt-6-luna** scores best on the checks and costs 40× less, but runs outside the EU, so it's the last fallback rather than the primary (open question 4).
- **The cost of 3.8 Flash is mostly uncached input.** On Vertex AI it read only 2% of its input from the implicit prompt cache. In probes it cached nothing on requests under about 4K tokens and was inconsistent above, and our static prefix (instructions and tools) is about 3.4K tokens. We don't pad the prompt to reach a cache minimum. The history is append-only, so the models that cache keep their prefix: PydanticAI's cache-collapse detector stays at zero for them; Flash-Lite's collapses come from Gemini's best-effort implicit cache.
- **Latency is the trade-off.** p50 is about 3 s, but p95 varied between 10 and 16 s across runs of 3.8 Flash on Vertex's shared capacity. The arena runs again on the final prompt before the demo.

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
