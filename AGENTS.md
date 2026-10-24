# AGENTS.md

Guidance for any coding agent (or human) working in this repository.

## What this is

`candidate-screening-agent`: an AI screening agent for delivery-driver applicants of a restaurant chain (fictional client "Grupo Sazón", Spain and Mexico). A candidate chats with the agent in a messaging-style web chat, and recruiters track outcomes in a recruiter tool.

- **Source of truth for behaviour:** `docs/process-design.md`. Names in code come from it: *knockout*, *gap*, *review reason* (⚑), *outcome*, *status*.
- `docs/` holds deliverables only. Working notes, specs and plans go in `.notes/` (gitignored).

## Core principles

- **The prompt drives the conversation; code owns the facts and the outcome.** One conversational agent follows the process design through its instructions and is verified by evals. Its tools validate what we own (service areas, dates), save it, and compute the outcome from the saved state. No model output sets an outcome or a status on its own.
- **`unknown` is not `false`.** No thresholds the process design doesn't define. No tone adaptation based on who the candidate seems to be.
- **Templates only where the channel requires them:** the first message and the reminders, which fall outside WhatsApp's 24-hour window. The agent writes everything else. Quick replies come from code (for now only Start / Not interested on the first message).
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
- **Use libraries the way their docs show** (PydanticAI, FastAPI, SQLAlchemy, Logfire, Pydantic Evals): documented APIs and defaults, no custom wrappers around them. Deviate only with a comment saying why.
- **Read top to bottom.** One flow (a tool, an endpoint, a turn) reads in one function from start to end. Extract a helper only when it's reused or names a real concept, never as a single-use step the reader has to jump into. Use full-word names (`state`, `answers`), never one-letter ones.
- **Results travel through return values,** not through side effects that a later step inspects.
- **Write the least new code that works.** Before writing, ask whether it needs to exist, and whether the codebase, the standard library or an installed dependency already does it. Deleting code beats adding it. No speculative options, wrappers or config. Never cut validation at trust boundaries, error handling, or security.

## Tests

Write tests where they pay off:
- **Unit tests for deterministic logic:** the outcome rules, postal-code and place lookup, date resolution, what the tools accept and reject, status transitions. Pure code, mostly without the DB.
- **A few turn-orchestration tests with the LLM stubbed:** PydanticAI `TestModel` / `FunctionModel` via `agent.override`, with `models.ALLOW_MODEL_REQUESTS = False` in `conftest.py`.
- **One end-to-end happy path through the API:** apply → chat → outcome.
- **Conversational behaviour is covered by evals** (`backend/evals/`, Pydantic Evals), not by unit tests. The evals are where the process design is enforced: outcome, saved fields, knockouts first, at most 2 tries per field, nothing asked twice, short messages with the question last, language and regional terms.
- **No tests for trivial code:** models without logic, config loading, `__init__`, framework behaviour.

## LLM, prompts and evals

- Prompts live in the repo (`backend/src/screening/prompts/`) and stay client-agnostic: client knowledge (FAQ, regional glossary, persona) lives in the client config and is rendered into the instructions.
- The prompt version is a hash of the rendered instructions (client data included) and the model settings, recorded on every turn and eval run. A changed FAQ is a changed prompt.
- Prompts, tool names and descriptions are in English. The instructions carry the language rules and the client's regional glossary; each turn's state block gives the country of work.
- Set model settings explicitly per agent (thinking, temperature). Don't rely on provider defaults.
- **Candidate data is processed in the EU** (Vertex AI `eu` multi-region). The one exception is the last fallback model, used only when the primary fails (see the README). Eval-only components (simulator, judge) see synthetic data only and may use other endpoints.
- Spanish quality is checked by evals (regional terms, gender-neutral wording, fluency), because reviewers may not speak Spanish.

**Changing the prompt**
- **No eval-specific patches.** Never add a phrase, a name or an answer to the prompt just to make one case pass. Fix the general rule or the data instead, unless the item genuinely belongs in the prompt.
- **Few-shot examples and eval cases stay separate.** Never paste a failing exchange into the prompt as an example and then test on that same case: that measures memorization. Write a different example that teaches the general behaviour, or vary the case in the eval. The failing case stays as a held-out check.
- **Every observed failure becomes an eval case first, then the fix.** Judge the fix on the whole set, not only on that case.

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
- Never commit secrets, `.env*` files, `deploy.env` or anything in `.notes/`. Variable names are documented in `backend/example.env`, kept in sync with the code.
