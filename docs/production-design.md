# Production design

*Design only: nothing here is built. It covers what the demo leaves out: the ATS integration, WhatsApp, scaling to 10K candidates a week, monitoring, retention, and the next steps. What is built is in the [README](../README.md).*

Numbers come from the evals (`backend/evals/results/arena.md`), the code or the README's measurements. Everything else is marked as an estimate.

## ATS integration

Which ATS Grupo Sazón uses, and whether it can push applications, is still open (open question 1 in the [process design](process-design.md)). So the spec below is ours and generic: the ATS, or a small connector per ATS, calls us, and we call a webhook back. If the ATS can only be polled, the connector polls it and calls the same endpoint.

### In: a new application

`POST /api/ats/applications`

```json
{
  "submission_id": "8812345",
  "full_name": "José Hernández Ruiz",
  "phone": "+52 81 0000 0000",
  "vacancy": "REQ-2291",
  "language": "es",
  "whatsapp_opt_in": true
}
```

- **Idempotency:** `submission_id`, the ATS's application id, is stored as `external_id = ats:{client}:{submission_id}`, which is unique. A retried push returns the same candidate and never starts a second chat, as `POST /api/applications` already does with `apply-form:{submission_id}`.
- **Vacancy:** the ATS's requisition id, mapped to one of our locations in the client config.
- **Responses:** `201 {candidate_id, continue_url}`, the same on a repeat, where `continue_url` is the "Continue on WhatsApp" link to show right after applying. `422` for an unmapped vacancy or an invalid phone, which also raises an alert. `401` for bad credentials.
- **Two more inbound calls:** `POST /api/ats/applications/{submission_id}/decision {outcome, by, note}`, the recruiter tool's decision route for recruiters who decide in the ATS, and `POST …/close {reason}`, for a filled vacancy or an application withdrawn in the ATS, which stops the reminders and tells the candidate with a template.

### Out: events on finish and on a decision

We `POST` events to a webhook URL per client.

| Event | When | Carries |
|---|---|---|
| `screening.finished` | The rules set an outcome | The outcome and its reasons, the answers, the matched restaurant, the summary, open questions, the talent-pool answer |
| `decision.recorded` | A recruiter decides (here or in the ATS) | The decision, who, the note, when, and the rules' outcome next to it |
| `screening.closed` | The candidate withdraws, or the chat is abandoned | The status and the stage where they stopped |

```json
{
  "event_id": "0b6f6c1e-4f2a-4c55-9d43-2f1f0c7a9e10",
  "type": "screening.finished",
  "occurred_at": "2026-10-26T10:14:03Z",
  "submission_id": "8812345",
  "candidate_id": "5d0c1b7e-8f0a-4a8e-b0d4-6c2f3e9a1b22",
  "outcome": "needs_human_review",
  "reasons": ["license unclear: license from Venezuela, validity unknown"],
  "answers": {
    "full_name": "José Hernández Ruiz",
    "license": null,
    "license_note": null,
    "area": {"location_id": "mx-mty-centro", "postal_code": "64060", "place": "Obispado"},
    "availability": ["part_time", "weekends"],
    "schedule": ["evening"],
    "experience": {"years": 2, "platforms": ["Rappi", "DiDi Food"]},
    "start_date": "2026-11-02"
  },
  "unclear": {"license": "license from Venezuela, validity unknown"},
  "marks": {"full_name": "saved", "license": "review", "service_area": "saved", "availability": "saved",
            "schedule": "saved", "experience": "saved", "start_date": "saved"},
  "summary": {"caveats": ["Verificar si la licencia venezolana es válida para conducir en México."], "notes": []},
  "open_questions": [],
  "talent_pool_opt_in": null,
  "language": "es",
  "prompt_version": "f32df8d2"
}
```

- `answers` and `unclear` are the saved state as stored (`ScreeningState` in `domain.py`), and `marks` is the recruiter page's ✓ / gap / ⚑ per field: values the ATS can map, not display text.
- The summary is in the recruiters' language (`recruiter_language`, Spanish here). The caveat reads "Check whether the Venezuelan license is valid for driving in Mexico."
- The event is queued in the transaction that stores the summary, written right after the close, or without it (`"summary": null`) when the summary agent fails.
- `screening.closed` isn't final, since a late reply reopens an abandoned chat.
- The transcript stays with us, in the recruiter tool or behind an authenticated link.

### Auth, retries and field mapping

- **Auth:**
  - In: a key per client in the `Authorization` header, stored hashed. Two keys can be active at once, so rotating one doesn't break the integration.
  - Out: each webhook is signed with HMAC-SHA256 over a timestamp and the body, with a secret per client. The ATS checks the signature and rejects old timestamps (replays). If the ATS can't check signatures, a bearer key instead.
- **Retries through an outbox:**
  - Every outbound event is written to an `outbox` table in the same transaction as the change behind it (the finishing turn, the summary, the decision). A webhook sent after the commit is lost if the process stops in between, and one sent before it can announce a change that rolled back.
  - A sender posts what's pending and marks it sent on a 2xx: a step in `tick()` today, a queue at scale.
  - A 5xx, a timeout or a 429 is retried with exponential backoff for up to 24 hours (a proposal). After that, and at once on any other 4xx, the event is parked and raises an alert.
  - Delivery is at least once. The ATS dedups on `event_id`, and a per-candidate sequence number keeps the order.
- **Field mapping** per client is data, in the client config like the locations and templates: requisition id → `vacancy_location_id`, our outcomes and statuses → the ATS's stages, our fields → the ATS's fields (custom ones included), and language codes. A connector per ATS vendor only where its API differs (auth, endpoints, polling); the events stay the same.

## Channels: WhatsApp

The core already speaks "inbound message → outbound messages" through the `Channel` Protocol (`channels.py`); the web chat is its first adapter. WhatsApp, through Meta's Cloud API, is the second.

- **Inbound** comes from Meta's webhook. We check its `X-Hub-Signature-256` signature (HMAC with the app secret), find the conversation by phone number, call `receive()` as the web chat does, and answer 200 at once. The WhatsApp message id becomes our `client_id`, stored once per conversation, so a redelivery is stored once.
- **Templates outside the 24-hour window.**
  - The first message, the reminders, the decisions and the failure notice are already templates per locale in the client config. Each becomes a template approved by Meta.
  - The adapter sends free text only within 24 hours of the candidate's last message. A turn answers the candidate's own message, so it falls inside the window unless a retry comes more than 24 hours late.
  - A recruiter's reply to a forwarded question may come later, so it's a template too.
- **The phone number as identity.** Today a chat is one application, so the same person applying twice gets two chats. On WhatsApp the number addresses a person, so there's a `people` table (number in E.164, opt-in, STOP, language), and `candidates` stay applications, each pointing to a person. An inbound message goes to the person's latest open application, and STOP stops every template to any of them.
- **Voice notes as media,** on the web chat's path ([README, Voice notes](../README.md#voice-notes)). An `audio` message carries a media id: we download it from the Graph API, transcribe it, drop the audio, and call `receive()` with the transcript. The reply goes back as an audio message, made from its text the same way.
- **Reply buttons.**
  - A template's buttons are fixed when Meta approves it: Start / Not interested on the first message.
  - Inside the window, interactive reply buttons are free, up to 3, with titles of up to 20 characters. The web chat's suggested replies map onto them.
  - Next step: the agent writes them, as structured output (`messages` plus `quick_replies` within those limits) in the same request. On knockout questions the buttons always include "not sure", so a tap can't turn an unknown into a no. Free text always works.
- **Delivery-status webhooks** (`sent`, `delivered`, `read`, `failed`) update each message. A first message that fails (say, the number isn't on WhatsApp) falls back to SMS with the chat link, as the process design says, and drop-off "before replying" splits into not delivered, delivered and read.

Nothing changes in the agent. The adapter renders each paragraph as its own message, and skips what WhatsApp can't show.

## Scaling to 10K candidates a week

### The arithmetic

Per completed screening, from terra's row in the model round (`20261025-0939`, its totals over 16 completed screenings): 8.8 turns and 16 model requests, 66K input tokens with 97% read from the prompt cache, 420 output tokens, and $0.0230; the summary agent's one call isn't included. The eval candidates are synthetic and cooperative, so the turn count is a floor.

| Per week, if all 10,000 applicants complete | | Basis |
|---|---|---|
| Completed screenings | 10,000 | The brief's volume, as an upper bound: today's calls reach about 40% |
| Turns | ~88,000 | 8.8 per screening (evals) |
| Model requests | ~160,000 + 10,000 summaries | 16 per screening (evals), plus one summary each |
| Input / output tokens | ~660M / ~4M | 66K / 420 per screening (evals); 97% of input from the cache |
| Agent cost | ~$230 (about $1,000 a month) | $0.0230 per completed screening (evals) |
| Peak load | ~0.9 turns/s, ~100 requests/min, ~400K input tokens/min | **Estimate:** 12 active hours a day, the peak hour at 3× the average |
| Turns running at once at the peak | 2 to 3.5 | **Estimate:** 0.9 turns/s × 2.6 s (p50) to 3.9 s (p95) per turn (evals, row `20261026-0848` with 49 cases × 1) |

Voice isn't included: ElevenLabs bills calls, transcription and speech on its own, and how many candidates would speak is unknown.

So the model traffic is small: throughput isn't what limits one instance first.

### What breaks first in today's single instance

1. **Availability.** One instance is one point of failure: a crash or a deploy pauses every conversation until it's back. Nothing is lost, since messages are stored before they're answered and `resume()` answers what's pending at startup, but candidates wait.
2. **Open connections.** Every open web chat holds an SSE request, and every call a WebSocket. `deploy.sh` sets no `--concurrency`, so Cloud Run's default of 80 per vCPU applies, here 80. With `--max-instances 1`, the next request waits at least 10 seconds and then gets a 429, candidates' messages and the recruiter tool included. **Estimate:** at 10K a week, about 360 screenings start in the peak hour, and at about 5 minutes each that's around 30 streams, plus tabs left open. Under the limit, but the first hard one a busy web chat hits. On WhatsApp, candidates hold no connection.
3. **In-process state** rules out a second instance. Each conversation's worker and lock, the mark a new message puts on a running turn, the SSE hub and the voice tickets live in memory. With two instances, two messages could start a worker on each, and two replies, and a reply could be published on the instance that doesn't hold the candidate's stream. `tick()` is already safe on several instances, under a Postgres advisory lock.
4. **Migrations** run at container start (the Dockerfile's `alembic upgrade head`), which is safe only with one instance.
5. **Recruiter metrics** scan every candidate on each request (`service.metrics()` has no time window). At about 500K applications a year, they need a window or a daily rollup.

The database connections aren't the bottleneck. No session is held across a model call: the worker reads, closes the session, runs the turn, then stores it in a new one. So SQLAlchemy's default pool (5 connections + 10 overflow) carries far more than the 2 to 3.5 turns running at once.

### What replaces it

- **A queue for the workers.** The pending messages in Postgres already are the queue; what changes is who takes a conversation.
  - A stored message creates a Cloud Tasks task for its conversation, scheduled after the debounce (`scheduleTime`), which calls a turn endpoint authenticated with OIDC.
  - The handler claims the conversation with a lease, a conditional `UPDATE` of a `turn_lease_until` column, as `decide()` already claims a decision. The lease outlives the turn deadline (35 s), so a crashed worker can't block the conversation for long.
  - A busy conversation fails the task, and Cloud Tasks retries it with backoff. A task that finds nothing pending does nothing, so duplicates are harmless.
  - The queue's dispatch limits cap our rate against the providers' (below). Summaries get their own, slower queue.
  - `tick()` moves to Cloud Scheduler, calling an authenticated endpoint every minute. The advisory lock stays.
  - Rejected: Pub/Sub, whose messages have no delivery time to schedule the debounce with (the only delay is the retry backoff after a nack, best effort). And Redis with Celery, a second stateful system for about one turn a second. A Postgres-only queue (`FOR UPDATE SKIP LOCKED` plus `LISTEN/NOTIFY`) would also carry this load with no new service; Cloud Tasks wins on the built-in delay, retries and rate limits.
- **Several instances.** With turns running inside requests, Cloud Run scales on load.
  - Run at least 2 instances, so one crash or deploy doesn't pause every chat, and raise `--concurrency`, since idle SSE streams cost almost nothing.
  - Migrations run as a Cloud Run job before the new revision takes traffic.
  - Voice tickets move to a database row, single-use and expiring. A call takes the same lease as a text turn.
  - A message stored on another instance still supersedes a running turn. Before storing, the turn checks Postgres for a pending message newer than the ones it took, so no signal between instances is needed.
- **The SSE fan-out.** The instance that stores a reply may not hold the candidate's stream. After each commit, the writer sends `NOTIFY` with the candidate id and the last message id; every instance `LISTEN`s on one connection and wakes its local streams, which read the new rows from the database, as they already do after `Last-Event-ID`. Typing indicators go the same way, and losing one is fine. Postgres does this at our volume, so no Redis pub/sub, and on WhatsApp there's no fan-out.
- **The database:** Cloud SQL with high availability (a standby in another zone). The pool size times the number of instances stays under the instance's connection limit.

### Provider rate limits

- **Before launch:** the estimated peak is ~100 requests and ~400K input tokens a minute. Compare it with the OpenAI organization's limits for terra and the Gemini API project's; this repo doesn't record either.
- **Bursts of 429s** go to the fallback, since `FallbackModel` moves on any API error, rate limits included.
- **A sustained limit** moves all traffic to the fallback, slower (p50 4.0 s per turn against 2.6 s) and about 2.5× the cost per screening. The fallback-rate alert (below) catches it.
- **Dispatch limits** in Cloud Tasks keep us under the provider's, so a spike waits a few seconds in the queue instead of failing turns.
- **EU processing:** Gemini on Vertex AI needs reserved capacity (Provisioned Throughput) first ([README, model choice](../README.md#why-gpt-56-terra-with-gemini-38-flash-as-the-fallback)).

### Infrastructure as code

`deploy.sh` becomes Terraform. Its resources are what `deploy.sh` creates (the APIs, the service account and its roles, the Cloud SQL instance, database and user, the secrets and the Cloud Run service) and what scaling adds (the queues, the Scheduler job, the migration job, the uptime check and the budget alert).
Secret values are added outside Terraform, so they never land in its state. There's one state per environment (staging, production), and one per region too if Mexican data must stay in Mexico (open question 4), with the same module twice. CI builds the image once on merge and runs the checks and tests, runs the evals when the prompt version changes, then deploys by image digest to staging and production, with the migration job in between. The wrong-project guard in `deploy.sh` becomes a separate project per environment.

## Monitoring

### What Logfire already gives

- **Turns:** a span per turn, `screening turn {conversation_id}`, with the prompt version, the status before and after, the outcome, the decision, and `cut_off` for a voice turn cut short. Under it, PydanticAI's spans for each model request (the model that answered in `gen_ai.response.model`, tokens) and each tool call. A text run thrown away for a newer message keeps its spans, under one marked `superseded=true`.
- **Other spans:** per summary, per HTTP request (except `/api/health` and `/mcp/`), per SQL query, and per MCP tool call (the SDK's own).
- **Metrics from PydanticAI:** tokens and `operation.cost` in USD per model request, by model.
- **Logs:** errors (`turn failed for …`, `worker-… crashed`, `tick failed`, `summary failed for …`, `voice turn failed for …`), a line for a tick that did something, and a line at the end of each call.
- **Content:** prompt and completion text only with `LLM_CONTENT_LOGGING`, which is on in the demo and off by default.

### Errors and alerting

- **Backend: Logfire.** The alerts that matter are rates over spans we already send. Logfire's alerts are SQL over spans and metrics, on a schedule, to Slack, Opsgenie or a webhook, with templates for exceptions, HTTP 5xx and LLM errors. Sentry would be a second SDK for the same exceptions, and the turn-level rates would need the spans sent there too.
- **Frontend: Sentry, or another browser error reporter.** The chat and recruiter pages report no errors today, so a JavaScript error on a candidate's phone is invisible.
- **Service down:** `/api/health` is left out of the traces, so that's a Cloud Monitoring uptime check.

### The alerts that matter

Thresholds are proposals, to tune on real traffic.

| Alert | Signal | Why | Fires when |
|---|---|---|---|
| Fallback rate | Requests answered by a model other than the primary (`gen_ai.response.model`) | The primary is down or rate-limited | Over 5% for 15 min |
| "Give me a moment" rate | `turn failed` logs over screening turns, and candidates in "Our replies are failing" | Candidates wait on us | Over 2% of turns in 15 min, or any candidate whose replies keep failing |
| Review rate | Turns that complete a screening (`status_before` → `status_after`), by `outcome` | A prompt or model change shifts outcomes and the recruiters' load | A day at 2× the trailing 7-day rate |
| p95 per turn | `screening turn` span duration | 3.9 s in the evals; the turn deadline is 35 s | Over 8 s for 15 min |
| Reminder loop | A heartbeat per tick, and overdue reminders | No reminders, no retries | No tick in 5 min, or a reminder 10 min overdue outside quiet hours |
| Daily cost | Sum of `operation.cost` over 24 h | A runaway loop, a stuck fallback, a price change | Over 2× completed screenings × $0.023; the GCP budget alert stays as the backstop |

The reminder loop's alert needs a small addition, since a tick logs only when it did something and silence can mean nothing was due. Each tick records a counter, plus two gauges read from the database (overdue reminders, the oldest pending message).

### The evals as a gate

- **Prompt changes:** every change to the prompt version runs the simulated set before it merges. The version hashes both agents' instructions and model settings (the screening agent's and the summary agent's), `spoken.md` and `live_call.md`, the whole client config and the tool code, so an FAQ edit counts. The gate is the deterministic checks at 1.00 (as today), the foreign-license case 5/5, and judge scores within noise of the baseline. A run of every case costs about $2 with the judge: about $1 for the agent and the simulator, about $1 for the judge.
- **Model versions** go through the same gate.
- **Production failures:** each becomes a scripted case, then the fix (AGENTS.md), rewritten with made-up details: real candidate text never goes into the eval set.
- **Versioned turns:** every turn records its prompt version, so the alerts can be split by version, and a regression shows against the change that brought it.
- **Live traffic:** judging it is a different job. In production, a sample of turns gets a cheaper judge (Haiku or Sonnet), checked against the offline judge's labels, and its scores feed the alerts. It sees real candidate data, so it runs under the same processing rules as the agent. Judging every turn with the offline judge would cost more than the agent: on the judged run of prompt `435b6180`, the judge cost $0.93 and terra $0.71.

## Retention

### What we store, and what we don't

| Where | What |
|---|---|
| Postgres `candidates` | Name, phone, vacancy, language, WhatsApp opt-in, the chat token, the screening state (answers, unclear notes, review flags, open questions), the summary, the decision and its note, timestamps |
| Postgres `messages` | Every message both ways, calls included: the candidate's words as transcribed, ours cut to what was heard |
| Postgres `turns` | Each turn's model messages as sent (context block, candidate text, tool calls and results, replies), prompt version, models, usage, errors |
| Logfire | Spans with ids, timings, prompt version, status and outcome; prompt and completion text only with `LLM_CONTENT_LOGGING` |
| Cloud Logging | Request URLs, which carry chat tokens and the MCP token in the path; with `VOICE_DEBUG`, the ElevenLabs SDK's call events (ids and lengths, not transcripts) |
| Model providers | Each request, which holds the conversation so far, under their API terms |

Not stored: audio, on our side ([README, Where the audio goes](../README.md#where-the-audio-goes)), and anything the prompt forbids asking (ID numbers, bank details, health, nationality, age).

### ElevenLabs

What's built, deleted and kept there is in the README's [Where the audio goes](../README.md#where-the-audio-goes). Proposed:
- **Speech generations** deleted once they've played (the SDK's `history.delete`), or zero retention under an enterprise agreement (table below).
- **An EU-processed option** for real candidates, since the demo's audio is processed in the US. It's still to be found.
- **Their DPA,** for anything ElevenLabs keeps under its own terms, which is outside this design.

### Proposed retention periods

**These are proposals.** The periods, the legal basis, the notices and the right to contest an automated knockout are the client's decision with their counsel, under Spanish (GDPR) and Mexican data-protection law: open question 4 in the process design.

| Data | Proposed | Why |
|---|---|---|
| Screenings that ended without a hire (not qualified, withdrawn, abandoned) | Personal data deleted 6 months after the last activity; an anonymous row kept (vacancy, status, outcome, stage, timestamps) | Long enough to answer a contested knockout; the anonymous row keeps the metrics |
| Qualified candidates | Deleted once the ATS confirms it holds the outcome, at the latest after 6 months | The ATS is the system of record for hiring |
| Talent-pool opt-in | Name, phone, language and area, until the candidate withdraws or 12 months pass | What they agreed to, and nothing else |
| STOP | All contact stops at once; only a hash of the number and the date are kept | We never write again, without keeping who they are |
| `turns` (model messages) | Content deleted after 90 days; usage, models and timings stay | Debugging and new eval cases; `messages` follow the screening's period |
| Logfire traces | The project's retention; content logging off for real candidates unless there's a DPA | |
| Cloud Logging | A shorter bucket retention, and chat tokens out of URLs (a cookie after the first visit; WhatsApp has no token) | A logged token grants access to a chat |
| Audio | Never stored | |
| Speech generations at ElevenLabs | Deleted once played (the SDK's `history.delete`), or zero retention under an enterprise agreement | They hold the candidate's name and, in the recap, their answers |

A daily step in `tick()` deletes or anonymises what's past its period; messages and turns already cascade when a candidate row is deleted (`ON DELETE CASCADE`). An erasure request, from a recruiter or from the ATS, does the same for one candidate at once.

## Potential improvements

One line each, with where each is explained.

- **Interview slots:** the qualified close books a call from slots recruiters publish per location. Today it says the team will follow up. ([README, Potential improvements](../README.md#potential-improvements))
- **Quick replies written by the agent,** within WhatsApp's limits, with "not sure" on knockout questions. ([Channels](#channels-whatsapp))
- **EU processing on Vertex AI with reserved capacity,** for real candidates. ([README, model choice](../README.md#why-gpt-56-terra-with-gemini-38-flash-as-the-fallback), [Provider rate limits](#provider-rate-limits))
- **Our own streaming STT and TTS with voice activity detection,** in place of the hosted Speech Engine. It gives EU processing, and a TTS we can flush, which would start a tool turn's speech about a second sooner. ([README, Latency](../README.md#latency))
- **OAuth for MCP writes,** with our recruiter login as the authorization server, so a recruiter's assistant can record decisions. ([README, MCP server](../README.md#mcp-server-for-recruiters))
- **Per-recruiter SSO** in place of the shared password. ([README, limits of the demo's login](../README.md#recruiter-tool))
- **Rate limiting** on the login and the public endpoints. ([README, limits of the demo's login](../README.md#recruiter-tool))
- **A spend cap per conversation,** on top of the per-turn request limit. ([README, Timeouts, retries and limits](../README.md#timeouts-retries-and-limits))
- **The ATS integration, WhatsApp, scaling past one instance and the retention jobs** above.
