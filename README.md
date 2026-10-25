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

**Git hooks:** `uv run --directory backend pre-commit install` once. Before each commit they run ruff, ty, oxlint, tsc and a check for leftover debugger calls. The tests are run by hand.

**Tests:** `cd backend && uv run pytest`. The API and service tests run against the compose Postgres, in a `screening_test` database they create with the migrations; the LLM is stubbed, so tests never call a model.

**Configuration is environment-only.** `uv run --env-file .env` loads `backend/.env` into the process environment. The app settings, the Logfire SDK (`LOGFIRE_TOKEN`) and Google auth (`CLOUDSDK_CONFIG`) all read it from there, so there's no second loading mechanism to keep in sync. Logfire and GCP are optional: without a token, traces stay on the console. The frontend has no env file, because anything Vite exposes ends up in the public bundle.

**Chat with the agent in the terminal** (in memory, no database):

```bash
cd backend
uv run --env-file .env python -m screening.cli --vacancy mx-cdmx-roma --name "José Hernández" --show-tools
```

**Chat through the API** (the routes and models are in [`contract.py`](backend/src/screening/contract.py)): apply, open the event stream, then send messages from another terminal.

```bash
curl -sX POST localhost:8000/api/applications -H 'content-type: application/json' -d '{"submission_id": "'"$(uuidgen)"'",
  "full_name": "José Hernández Ruiz", "phone": "+52 55 0000 0000", "vacancy_location_id": "mx-cdmx-roma", "language": "es"}'
# → {"candidate_id": "…", "chat_url": "/chat/TOKEN"}
curl -N localhost:8000/api/chat/TOKEN/events             # message / typing / status events; replays after Last-Event-ID
curl -sX POST localhost:8000/api/chat/TOKEN/messages -H 'content-type: application/json' -d '{"text": "Empezar", "client_id": "1"}'
```

**A chat is one application.** The same person applying twice gets two chats; a WhatsApp channel, where the phone number is the identity, needs a person table separate from applications.

Each conversation has one worker in the API process. It waits for a burst of messages to settle (`CHAT_DEBOUNCE_SECONDS`), answers them in one turn, and stores the turn (the model messages, prompt version, models, usage) before publishing the reply. If every model fails, the candidate gets the "give me a moment" template once and the messages stay pending: their next message, a restart or the timed retry (see [Timed work](#timed-work-reminders-abandoned-chats-retries)) tries again. When the screening ends, the summary agent writes the recruiter's caveats and notes; the key data (every answer, the outcome and its reasons) are built by code from the saved state.

**The candidate pages** (`frontend/`: React, Vite, Tailwind, React Router, TanStack Query). `/apply` is the public apply form; with `APPLY_ACCESS_CODE` set, the link to share is `/apply?code=…`, which fills the code in. Its confirmation opens the chat, or shows a QR code to continue on a phone. `/chat/TOKEN` is the mobile-first chat: it renders the event stream (and replays what it missed after a reconnect), shows the first message's suggested replies, a typing indicator, and closes the input once the candidate withdraws. `frontend/src/api.ts` mirrors `contract.py`.

**The recruiter tool** is at `/recruiter` (see [Recruiter tool](#recruiter-tool)); `example.env` has a local password for it.

**Models and where candidate data goes.** The demo's live turns run on OpenAI's GPT-5.6 terra through OpenAI's API. If it fails (an outage, a run of rate limits, or a request past `REQUEST_TIMEOUT`), PydanticAI's `FallbackModel` hands the message to Gemini 3.8 Flash through the Gemini API: another provider, so one outage can't take out both. Neither keeps processing in the EU, which is fine for the demo's synthetic data. For real candidates, the EU path is Gemini on Vertex AI in the `eu` multi-region (`google-cloud:` models); on 2026-10-25 it stalled under shared quota (see "Why GPT-5.6 terra"), so it needs reserved capacity before it can carry live chats. Whether data may leave the EU is a client decision (open question 4 in the process design). Both models are set in `.env` (`SCREENING_MODEL`, `SCREENING_FALLBACK_MODELS`), with `OPENAI_API_KEY` and `GOOGLE_API_KEY`.

With one key, set `SCREENING_FALLBACK_MODELS=[]`: `OPENAI_API_KEY` alone runs the default model. Use these paths with test data only.

To run the production image locally: `docker compose --profile app up --build` (http://localhost:8080). It picks up `backend/.env` if present.

## Recruiter tool

`/recruiter` sits behind a login page that asks for a name and a shared password (`RECRUITER_PASSWORD`). The login sets a signed, HttpOnly session cookie (Starlette's `SessionMiddleware`, keyed with `SESSION_SECRET`, SameSite=Lax, 14 days, so reviewers can come back over several days). Recruiter writes are POST only, which with SameSite=Lax is the CSRF protection.

- **Tabs:** Ready (qualified), Review (waiting for a person, or our replies to the candidate keep failing), In progress, Closed (not qualified, withdrawn, abandoned), each with its count. Search by name or phone looks in every tab. A tab lists its latest 200 candidates; search reaches the rest.
- **A candidate's page:**
  - the key data, built by code from the saved answers, with a ✓, gap or ⚑ per field;
  - the AI summary under it;
  - the matching restaurant and the chat link to copy;
  - the transcript, with the templates we sent marked.

  It refreshes every 5 s, so a chat that's still running shows as it goes.
- **Decisions:** Qualified or Not qualified, with a note for the team.
  - The candidate is told in the chat with a template (`decision_qualified` / `decision_not_qualified` in the client config). The note stays internal.
  - The decision is final and has its own columns (`decided_outcome`, `decided_by`, `decision_note`, `decided_at`). The worker keeps writing the rules' outcome in `outcome`, so nothing it writes can overwrite a decision, and the page shows both.
  - The chat stays open after a decision, and the template goes into the model's history like every message we send.
- **Metrics:** one row above the tabs (`GET /api/recruiter/metrics`), computed from the database on each request.
  - *Response rate* (applicants who replied at least once), *completion rate* (of those, screenings that finished) and *drop-off* (abandoned candidates by where they stopped: before replying, at a field, or at the recap) answer "60% don't pick up".
  - *Sent to review* (finished screenings the rules sent to a person) and *overrides* show how much still takes a recruiter. An override is a decision that differs from a rules' outcome of qualified or not qualified; deciding a review case isn't one. The override rate is over the outcomes a recruiter decided on: of the ones they checked, how many they overturned. Most not-qualified outcomes are never opened, and counting them would hide disagreement.
  - *Median time to outcome*, from applying to the rules' outcome.
- **Interview slots aren't built.** The qualified close says the hiring team will contact the candidate about the next step; booking a call from slots that recruiters publish per location is a next step.

**Limits of the demo's login.**
- **The name is attribution, not identity:** with one shared password, anyone can type any name. In production, each recruiter signs in through the client's SSO.
- **Changing the password doesn't end open sessions,** because the cookie is stateless. After the review, rotate both: `RECRUITER_PASSWORD=… SESSION_SECRET=$(openssl rand -hex 32) ./deploy.sh`.
- **No rate limiting** on the login, nor yet on the public endpoints.
- **A decision during a running turn is rare and accepted:** the candidate may get the decision between two agent replies.

## Timeouts, retries and limits

A slow model must not leave a candidate waiting. A live turn has three bounds, all set in `.env`. They don't change what the model answers, so they're passed at run time and stay out of the prompt version.

| Bound | Default | What it does |
|---|---|---|
| `REQUEST_TIMEOUT` | 10 s | One deadline per request to one model, over every attempt the provider SDK makes (PydanticAI's `request_timeout`; the SDK's own `timeout` applies per attempt and each retry re-arms it). When it runs out, `FallbackModel` moves to the next model, which gets a fresh deadline. |
| `TURN_TIMEOUT` | 35 s | The whole turn (`agent.run(timeout=…)`). `FallbackModel` tries the primary first on every request, so a two-request turn that falls back twice takes two request deadlines plus two fallback answers: 10 + 7 + 10 + 7 s with the fallback at its p99 in the evals. |
| `TURN_REQUEST_LIMIT` | 7 | Model requests per turn (`UsageLimits.request_limit`). In the evals, a legitimate turn needed at most 5 (a postal code, a request for a person and a question in one message); the one that needed 9 was a model saving invented answers in a loop. |

**Retries**, from the inside out:
- **Provider SDK:** its defaults, inside the request deadline. PydanticAI's OpenAI and Anthropic clients retry twice; its Google client doesn't retry.
- **Fallback:** any model API error, a timed-out request included, goes to the next model in `SCREENING_FALLBACK_MODELS`.
- **Agent:** a tool call with invalid arguments goes back to the model with the validation error, once per tool (PydanticAI's default); a second invalid call fails the turn.

**What the candidate sees when a turn fails** (every model failed, the turn timed out, it hit the request limit, or storing it failed): the "give me a moment" template, once. Their messages stay pending, and their next message, a restart or the timed retry tries again.

**Spend.** There's no per-conversation budget in code; the request limit only bounds one turn. In production, spend is watched outside the app: a GCP budget alert (see Deployment) and a Logfire alert on model cost.

The summary agent gets the same request deadline. Evals run without the deadlines and without a fallback, so the latency they report is the model's own.

## Timed work: reminders, abandoned chats, retries

The lifespan calls `tick(now)` every 30 s. It takes `pg_try_advisory_xact_lock` first, so when two instances overlap (while Cloud Run rolls out a revision) only one of them ticks.

- **Reminders** go to invited and in-progress candidates who are silent, with nothing of theirs left to answer. They're timed from our last message that waits for an answer: 4 h and 1 day before the first reply, 2 h and 1 day mid-chat (`timings` in the client config).
  - Mid-chat, the reminder names where the chat stopped: the next field to ask, or the recap.
  - They're templates, because they fall outside WhatsApp's 24-hour window, and they're stored as turns, so the agent sees them.
  - None between 21:00 and 09:00 at the vacancy's local time: one that falls due then waits for 09:00.
  - A late tick (after a restart, say) sends one reminder, not every one it missed.
- ***Abandoned*** after 3 days of silence. A late reply reopens the chat where it stopped.
- **The timed retry:** pending messages older than 60 s with no worker on them (left by a failed turn or a stopped process) get a new turn. After 3 failed turns on the same messages it stops, because a turn that always fails (one that hits the request limit, say) would cost a model run every 30 s. The candidate then shows in the recruiter's Review tab as "Our replies are failing", and their next message or a restart tries again.
- **Summaries a restart lost:** a finished screening without a summary gets one. One that fails is tried again after the next restart, not on every tick.

The tick's queries run under `logfire.suppress_instrumentation()`, since twice a minute they would bury the turns in the traces; a tick that did something logs one line naming the candidates. Tests call `tick(now)` with the time they need, and there's no fast-forward endpoint. To show a reminder in a demo, tick once from a shell with the clock moved on by the hours given at the end (against the database in `.env`; the open chat shows it after a reload). It prints what the tick did:

```bash
cd backend && uv run --env-file .env python -c "
import asyncio, sys; from datetime import UTC, datetime, timedelta; from screening.main import create_app
print(asyncio.run(create_app().state.service.tick(datetime.now(UTC) + timedelta(hours=float(sys.argv[1])))))" 5
```

Quiet hours are checked at the moved clock, too. From Madrid after 16:00, `5` lands past 21:00, and the tick sends nothing (`reminded=[]`). Pick hours that keep the moved clock between 09:00 and 21:00 at the vacancy's local time: at least 4 before the candidate's first reply, at least 2 mid-chat.

## Evals

The process design is enforced by evals, not unit tests. `backend/evals/` (Pydantic Evals) runs synthetic candidates from `cases.yaml`, in both countries and in Spanish and English, against the real agent. A simulated candidate (an LLM playing the persona) chats with the agent in memory until the conversation ends, and labels what each agent message asked for; then the summary agent writes the recruiter summary. Then:

- **Deterministic checks** carry the process rules: the outcome and review reasons, the saved answers, knockouts asked first, nothing asked again, at most two tries per field, message shape (at most 3 messages of 40 words, one question, last), no acknowledgement sent as a message on its own, the language saved at the end, the country's regional terms and gender-neutral wording. They decide the ranking.
- **An LLM judge** (OpenAI `gpt-6-luna`) scores tone, naturalness, language quality (including whether each reply follows the candidate's language) and the recruiter summary (nothing in it missing from the transcript or contradicting it). Its scores on OpenAI rows are marked: judges tend to favour their own family's style.
- **Per config:** the thinking config the provider actually receives, p50/p95 latency per turn, cost per completed screening (genai-prices), the share of input tokens read from the provider's prompt cache, cache collapses flagged by PydanticAI's instrumentation, and which model answered each turn.
- **Scripted cases** replay an observed failure: fixed candidate messages instead of the simulator, checked on the state after the last message. They're cheap to rerun many times and their input never changes. The simulated set covers whole conversations and runs before a prompt change is committed.

**What the numbers can and can't show.**
- The runs aren't deterministic: the simulator and the agent both sample. With 18 runs per row, one run moves a rate by 0.06. On the same prompt, 3.8 Flash scored 0.72 and 0.89 on `no_standalone_ack` in two runs, and its p95 ranged from 8 to 16 s. Rank on the real errors you read in the transcripts, not on decimals.
- The process checks are code, but they read the simulator's labels (`asked_about`). A wrong label shows up as a failure.

```bash
cd backend
uv run --group evals --env-file .env python -m evals.run --model openai:gpt-5.6-terra                     # one model
uv run --group evals --env-file .env python -m evals.run --model openai:gpt-5.6-terra \
    --fallback google:gemini-3.8-flash                                                                 # the demo config
uv run --group evals --env-file .env python -m evals.run --model A --model B --repeat 2                # the arena
uv run --group evals --env-file .env python -m evals.run --model A \
    --case scripted_mx_foreign_license_not_a_no --repeat 5                                             # one scripted case
```

`--settings '{"temperature": 0.2}'` merges into the model settings. Rate limits are retried with backoff (Pydantic Evals' `retry_task`), never handed to another model, so a row measures one config. Results are appended to [`backend/evals/results/arena.md`](backend/evals/results/arena.md); full transcripts land in `backend/evals/results/transcripts/` (not committed). The simulator and the judge only see synthetic data, so they may run outside the EU.

### Why GPT-5.6 terra, with Gemini 3.8 Flash as the fallback

The round on 2026-10-25 (prompt `da96a191`; full tables in [`arena.md`](backend/evals/results/arena.md)) had two stages. First a gate: the scripted foreign-license case 5 times per config. A license from another country whose validity the candidate doesn't know must end up *unclear* after two tries, never a "no". Then the configs that passed ran the full set (10 cases × 2).

| Gate: license case ×5 | Thinking sent | Passed |
|---|---|---|
| Gemini 3.8 Flash, on Vertex AI `eu` and on the Gemini API | `thinking_level` LOW | 5/5, 5/5 |
| GPT-5.6 terra | reasoning effort none | 5/5 |
| gpt-6-luna | reasoning effort none / low | 5/5 / **3/5** |
| Claude Haiku 5.5, prompt cache on | thinking disabled / adaptive at effort low | **3/5** / **2/5** |

| Full set | GPT-5.6 terra | Gemini 3.8 Flash (Gemini API) | gpt-6-luna |
|---|---|---|---|
| Outcome right | 1.00 | 1.00 | 1.00 |
| All process checks | 1.00 | 0.98 | 0.99 |
| Judge: tone / naturalness / language | 0.93 / 0.82 / 1.00 * | 0.91 / 0.86 / 1.00 | 0.93 / 0.79 / 0.99 * |
| p50 / p95 per turn | 2.6 s / 3.8 s | 4.0 s / 7.0 s | 3.0 s / 5.5 s |
| Request p99 | 2.4 s | 7.0 s | 3.3 s |
| Input read from the prompt cache | 97% | 7% | 97% |
| Cost per completed screening | $0.022 | $0.057 | $0.001 |

\* judged by a model of the same family.

- **Models that save answers the candidate never gave are out.** gpt-6-luna and Haiku 5.5 at low effort did: a license "yes" saved during the name question, before the license was asked. In an earlier run, luna invented a whole profile from "Sure, go ahead" and closed it as qualified. The checks only catch this when the field comes up again, so luna's 0.99 understates it. That's why it's out despite costing 18× less than terra.
- **terra and 3.8 Flash tie on the checks and the judge.** terra is faster and 2.6× cheaper, mostly because OpenAI's prompt cache holds the append-only history (97% of input), while 3.8 Flash's best-effort implicit cache read 7%. 3.8 Flash's misses are style only: two questions in a turn, a "Gracias, Luis." ("Thanks, Luis.") sent on its own.
- **3.8 Flash is the fallback:** another provider, so one outage can't take out both. Its request p99 (7.0 s) fits the 10 s request deadline.
- **Not Vertex `eu` for the demo.** The same day, 3.8 Flash on Vertex `eu` (dynamic shared quota) took up to 85 s per turn, then requests hung for 40 minutes; the Gemini API answered in about 2 s. The EU path needs reserved capacity first.
- **The round before** (2026-10-24, EU models only) ruled out Gemini 3.5 Flash (it saved the name from the application before the candidate confirmed it) and 3.5 Flash-Lite (a wrongful license knockout, and an area asked three times).
- **Cost:** about $22 per 1,000 completed screenings on terra, against about $57 on 3.8 Flash at its launch price. The summary agent adds one call per screening.

## Deployment (GCP)

A single Cloud Run service runs the API and the built frontend, with Cloud SQL (Postgres 18) and Secret Manager. The models are called through OpenAI's API and the Gemini API (keys in Secret Manager); Vertex AI stays enabled for the EU path.

```bash
cp deploy.env.example deploy.env   # set GCP_PROJECT (and CLOUDSDK_CONFIG if you use a dedicated gcloud config)
./deploy.sh
```

`deploy.sh` is re-runnable. It refuses to run if the active gcloud project differs from `GCP_PROJECT`. It enables the required APIs and creates the service account, the Cloud SQL instance, the database and its secret only if they're missing, then deploys from source with Cloud Build.

The demo deployment logs full LLM prompts and completions to Logfire (`LLM_CONTENT_LOGGING=true`), so every turn of a test chat can be read in the traces. People trying the demo might still type real details, so the apply page asks for made-up data. For a real client it stays off unless there's a DPA and a legal basis.

Secrets: `OPENAI_API_KEY` is required; `GOOGLE_API_KEY` (the fallback), `APPLY_ACCESS_CODE`, `RECRUITER_PASSWORD` and `LOGFIRE_TOKEN` are optional, and `SESSION_SECRET` is generated on the first deploy. `deploy.sh` stores each in Secret Manager (see `deploy.env.example`).

The service runs as exactly one always-on instance (`--min-instances 1 --max-instances 1 --no-cpu-throttling`), because the reminder loop, the SSE streams and the per-conversation locks live in that process.

**Budget alert.** Run this once, as protection against a runaway loop. The amount must be in the billing account's currency:

```bash
gcloud billing budgets create --billing-account=BILLING_ACCOUNT_ID \
  --display-name="candidate-screening-agent" --budget-amount=50EUR \
  --filter-projects=projects/$GCP_PROJECT \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```
