# AGENTS.md

Guidance for any coding agent (or human) working in this repository.

## What this is

`candidate-screening-agent`: an AI screening agent for delivery-driver applicants of a restaurant chain (fictional client "Grupo Sazón", Spain and Mexico). A candidate chats with the agent in a messaging-style web chat, and recruiters track outcomes in a recruiter tool.

- **Source of truth for behaviour:** `docs/process-design.md`. Names in code come from it: *knockout*, *gap*, *review reason* (⚑), *outcome*, *status*.
- `docs/` holds deliverables only. Working notes, specs and plans go in `.notes/` (gitignored).

## Core principles

- **The LLM understands, code decides.** The model extracts what the candidate said and phrases replies. Validation, the 2-try policy, knockouts, review reasons, outcomes and status transitions are deterministic, tested code. No model output sets an outcome or a status.
- **`unknown` is not `false`.** No thresholds the process design doesn't define. No tone adaptation based on who the candidate seems to be.
- **Load-bearing messages are templates** (first contact, AI disclosure, reminders, recap, outcome messages, failure notice): per-locale strings rendered by code. The LLM writes only the conversational middle. Quick replies always come from code.
- **The channel is an adapter.** The core speaks `inbound message → outbound messages` (template or free text). The web chat is the first adapter.
- **Client configuration is data** (locations, service areas, locales, persona, timings, FAQ, templates). The screening schema is a typed Pydantic model, not a form engine.

## Engineering rules

- **No over-engineering.** Every module, abstraction or feature must name the concrete need it serves today. A Protocol or interface only where a second implementation is real (the channel adapter). No generic frameworks, plugin systems, DI containers, repository layers or layers "for later".
- **No infrastructure the problem doesn't need:** no Redis, Celery, brokers or queues. In-process asyncio plus Postgres is the design; scaling goes in the README.
- **Few, well-defined modules.** Don't scatter code across many tiny files.
- **Typed end to end.** Type hints everywhere, Pydantic at the boundaries (API, config, LLM I/O, JSONB). Enums and config instead of hardcoded strings.
- **Comments explain *why*, never *what*.**
- **Errors are handled at the edges** (API, channel adapter, worker). The domain stays simple and raises nothing for expected candidate input.
- **Check library APIs against the docs for the installed version** before using them (PydanticAI is 2.x; don't rely on memory of 1.x). Prefer reading the installed source when the docs are unclear.

## Tests

Write tests where they pay off:
- **Unit tests for deterministic logic:** qualification rules, field validation, the 2-try policy, postal-code lookup, date resolution, status transitions. Pure code, mostly without the DB.
- **A few turn-orchestration tests with the LLM stubbed:** PydanticAI `TestModel` / `FunctionModel` via `agent.override`, with `models.ALLOW_MODEL_REQUESTS = False` in `conftest.py`.
- **One end-to-end happy path through the API:** apply → chat → outcome.
- **Conversational behaviour is covered by evals** (`backend/evals/`, Pydantic Evals), not by unit tests.
- **No tests for trivial code:** models without logic, config loading, `__init__`, framework behaviour.

## LLM, prompts and evals

- Prompts live in the repo (`backend/src/screening/prompts/`). The prompt version is a content hash recorded on every turn and eval run.
- Prompts, tool names and descriptions are in English. Code sets the reply language every turn; locale wording and few-shot examples come from the locale config.
- Set model settings explicitly per agent (thinking, temperature). Don't rely on provider defaults.
- **Candidate data is processed in the EU only** (Vertex AI `eu` multi-region). Eval-only components (simulator, judge, benchmarks) see synthetic data only and may use other endpoints.
- Spanish quality is checked by evals (regional terms, gender-neutral wording, fluency), because reviewers may not speak Spanish.

## Observability and privacy

- Logfire instruments FastAPI, SQLAlchemy and PydanticAI, and adds a span per conversation turn with the ids, state transitions and prompt version. `send_to_logfire='if-token-present'`: the app and tests run without a token.
- LLM prompt and completion content is candidate PII. Its logging is a config switch, off by default in deployed environments.
- Product analytics (funnel, drop-off) come from the database, not from Logfire.

## Cloud access (GCP)

- **Never use the machine's default gcloud configuration or ADC.** Every command that touches GCP (gcloud, running the app or evals against Vertex) must set `CLOUDSDK_CONFIG` to the project-specific directory, loaded from a gitignored env file (`.env`, `deploy.env`).
- Before the first GCP command in a session, run `gcloud config list` and confirm that the active project is the expected one.
- Committed scripts stay generic: project ids, accounts and personal paths come from the environment or gitignored files only. `deploy.sh` refuses to run if the active project doesn't match `GCP_PROJECT`, and never creates projects.

## Git

- Small, meaningful commits in conventional style (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Never commit secrets, `.env*` files, `deploy.env` or anything in `.notes/`.
