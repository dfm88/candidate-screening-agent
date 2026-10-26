"""Demo data on a deployed app: the simulator's personas apply and chat through the public API, as candidates do, so
the turns, summaries, outcomes and metrics are real. A few candidates apply and never reply, one gives up halfway, and a
recruiter decides two finished screenings, so every tab and metric has something in it (abandoned needs 3 days).

    APPLY_ACCESS_CODE=… RECRUITER_PASSWORD=… uv run --group evals --env-file .env python -m evals.demo_data URL

The access code and the recruiter password come from the environment: on prod, piped from Secret Manager, never
printed. The simulator runs here (OPENAI_API_KEY); the app answers with its own models. Each conversation is written to
a markdown file with the recruiter's view of it, the source for docs/sample-conversations.md.
"""

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic_ai import ModelResponse
from pydantic_ai.models import Model
from pydantic_evals import Dataset

from evals.run import CASES, RESULTS, SIMULATOR
from evals.task import SIMULATOR_SETTINGS, Expected, Persona, ScreeningRun, candidate_prompt, response_cost, simulator
from screening.agent import model_from_string
from screening.config import Language, Settings
from screening.contract import (
    ApplicationCreated,
    ApplicationRequest,
    Author,
    CandidateDetail,
    CandidateMessage,
    ChatMessage,
    DecisionRequest,
    Login,
    Typing,
)
from screening.domain import Outcome, Status

# The candidate who gives up halfway: this persona stops replying after these many turns.
GIVES_UP_AFTER = {"mx_monterrey_name_only": 3}
# Apply and never reply, so the response rate isn't 100%: name, vacancy, language.
NEVER_REPLY = [
    ("Laura Gómez Sanz", "es-mad-getafe", Language.ES),
    ("Héctor Ramírez Luna", "mx-cdmx-coyoacan", Language.ES),
    ("Emma Clarke", "es-bcn-gracia", Language.EN),
]
RECRUITER = "Demo script"
# A review the recruiter resolves, and an override of a rules' qualified, so Ready and the overrides aren't empty.
DECISIONS = [
    (
        Outcome.NEEDS_HUMAN_REVIEW,
        DecisionRequest(outcome=Outcome.QUALIFIED, note="Revisado por teléfono: puede seguir al siguiente paso."),
    ),
    (
        Outcome.QUALIFIED,
        DecisionRequest(outcome=Outcome.NOT_QUALIFIED, note="Este restaurante cubrió sus puestos esta semana."),
    ),
]
# The worker waits CHAT_DEBOUNCE_SECONDS (1.5 s) for a burst to settle before it starts typing: quiet for longer than
# that, with nothing typing, means the agent has answered.
QUIET_SECONDS = 4
TURN_DEADLINE_SECONDS = 120


async def read_events(client: httpx.AsyncClient, token: str, events: asyncio.Queue[tuple[str, dict]]) -> None:
    """The chat's event stream, as the web chat reads it: reconnects after a drop and resumes from Last-Event-ID."""
    last_id: int | None = None
    while True:
        headers = {"Last-Event-ID": str(last_id)} if last_id else {}
        try:
            async with client.stream("GET", f"/api/chat/{token}/events", headers=headers, timeout=None) as response:
                response.raise_for_status()
                event, data = "message", ""
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line.removeprefix("event:").strip()
                    elif line.startswith("data:"):
                        data += line.removeprefix("data:").strip()
                    elif line.startswith("id:"):
                        last_id = int(line.removeprefix("id:"))
                    elif not line and data:
                        await events.put((event, json.loads(data)))
                        event, data = "message", ""
        except httpx.HTTPError:
            await asyncio.sleep(1)


async def settle(events: asyncio.Queue[tuple[str, dict]], transcript: list[str]) -> None:
    """Take the chat's events until the agent has answered: nothing typing, and nothing new for a few seconds."""
    typing = False
    async with asyncio.timeout(TURN_DEADLINE_SECONDS):
        while True:
            try:
                event, data = await asyncio.wait_for(events.get(), timeout=QUIET_SECONDS)
            except TimeoutError:
                if typing:
                    continue
                return
            if event == "typing":
                typing = Typing.model_validate(data).active
            elif event == "message":
                message = ChatMessage.model_validate(data)
                transcript.append(f"{'Assistant' if message.author is Author.ASSISTANT else 'You'}: {message.text}")


async def apply(client: httpx.AsyncClient, request: ApplicationRequest) -> ApplicationCreated:
    response = await client.post("/api/applications", json=request.model_dump(mode="json"))
    response.raise_for_status()
    return ApplicationCreated.model_validate(response.json())


def application(
    name: str, vacancy: str, language: Language, number: int, access_code: str | None
) -> ApplicationRequest:
    """What the apply form sends, with a synthetic phone number."""
    phone = f"+34 600 000 {number:03d}" if vacancy.startswith("es-") else f"+52 55 0000 {number:04d}"
    return ApplicationRequest(
        submission_id=uuid.uuid4(),
        full_name=name,
        phone=phone,
        vacancy_location_id=vacancy,
        language=language,
        access_code=access_code,
    )


async def converse(
    client: httpx.AsyncClient, simulator_model: Model, persona: Persona, request: ApplicationRequest
) -> tuple[uuid.UUID, float]:
    """One candidate: apply, then chat turn by turn until the simulator is done. Returns the candidate's id and the
    simulator's spend."""
    created = await apply(client, request)
    token = created.chat_url.rsplit("/", 1)[1]
    events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    reader = asyncio.create_task(read_events(client, token, events))
    transcript: list[str] = []
    spend = 0.0
    try:
        await settle(events, transcript)
        for _ in range(persona.max_turns):
            result = await simulator.run(
                candidate_prompt("\n".join(transcript)),
                deps=persona,
                model=simulator_model,
                model_settings=SIMULATOR_SETTINGS,
            )
            spend += sum(response_cost(m) or 0 for m in result.new_messages() if isinstance(m, ModelResponse))
            for text in result.output.messages:
                message = CandidateMessage(text=text, client_id=uuid.uuid4().hex)
                response = await client.post(f"/api/chat/{token}/messages", json=message.model_dump(mode="json"))
                # A withdrawn chat takes no more messages.
                if response.status_code == 409:
                    return created.candidate_id, spend
                response.raise_for_status()
            if not result.output.messages:
                break
            await settle(events, transcript)
            if result.output.done:
                break
    finally:
        reader.cancel()
    return created.candidate_id, spend


def write_conversations(details: dict[str, CandidateDetail], path: Path) -> None:
    """Each conversation as the recruiter sees it: the key data, the summary, the decision and the transcript."""
    parts = [f"# Demo conversations · {datetime.now(UTC):%Y-%m-%d %H:%M} UTC\n"]
    for persona_name, detail in details.items():
        decision = f" · decided {detail.decision.outcome} by {detail.decision.by}" if detail.decision else ""
        parts.append(
            f"## {persona_name} · {detail.full_name} · {detail.vacancy.name}\n\n"
            f"Status: {detail.status} · rules' outcome: {detail.key_data.outcome or '–'}{decision}\n"
        )
        parts.append(
            "Key data: " + "; ".join(f"{field}: {answer}" for field, answer in detail.key_data.answers.items()) + "\n"
        )
        if detail.summary:
            parts.append(f"Summary: {detail.summary.model_dump_json()}\n")
        for message in detail.transcript:
            speaker = "candidate" if message.author is Author.CANDIDATE else "assistant"
            if message.template_key:
                speaker += f" (template {message.template_key})"
            parts.append(f"> **{speaker}** " + message.text.replace("\n", "\n> ") + "\n")
    path.write_text("\n".join(parts), encoding="utf-8")


async def main(args: argparse.Namespace) -> None:
    settings = Settings()
    simulator_model = model_from_string(args.simulator, settings)
    dataset = Dataset[Persona, ScreeningRun, Expected].from_file(CASES)
    now = datetime.now(UTC)
    # The simulated personas, on today's clock: the agent reads the real one.
    personas = {
        str(case.name): case.inputs.model_copy(
            update={"now": now, "max_turns": GIVES_UP_AFTER.get(case.name, case.inputs.max_turns)}
        )
        for case in dataset.cases
        if not case.inputs.script and (not args.case or case.name in args.case)
    }
    limit = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:

        async def one(number: int, name: str, persona: Persona) -> tuple[str, uuid.UUID, float] | None:
            request = application(
                persona.name_on_application, persona.vacancy, persona.application_language, number, access_code
            )
            async with limit:
                try:
                    candidate_id, spend = await converse(client, simulator_model, persona, request)
                except Exception as error:
                    # One conversation that fails (a turn past the deadline, say) leaves the others to finish.
                    print(f"failed: {name}: {error!r}")
                    return None
            print(f"done: {name}")
            return name, candidate_id, spend

        access_code = settings.apply_access_code
        ran = await asyncio.gather(*(one(n, name, p) for n, (name, p) in enumerate(personas.items(), start=1)))
        conversations = [conversation for conversation in ran if conversation]
        for number, (name, vacancy, language) in enumerate(NEVER_REPLY[: args.never_reply], start=101):
            await apply(client, application(name, vacancy, language, number, access_code))

        login = Login(name=RECRUITER, password=settings.recruiter_password or "")
        (await client.post("/api/recruiter/login", json=login.model_dump(mode="json"))).raise_for_status()

        async def detail(candidate_id: uuid.UUID) -> CandidateDetail:
            response = await client.get(f"/api/recruiter/candidates/{candidate_id}")
            response.raise_for_status()
            return CandidateDetail.model_validate(response.json())

        details = {name: await detail(candidate_id) for name, candidate_id, _ in conversations}
        for rules_outcome, decision in DECISIONS:
            chosen = next(
                (
                    name
                    for name, candidate in details.items()
                    if candidate.key_data.outcome is rules_outcome and candidate.status is Status.COMPLETED
                ),
                None,
            )
            if chosen is None:
                print(f"nobody with a rules' outcome of {rules_outcome} to decide")
                continue
            url = f"/api/recruiter/candidates/{details[chosen].id}/decision"
            (await client.post(url, json=decision.model_dump(mode="json"))).raise_for_status()
            details[chosen] = await detail(details[chosen].id)

    write_conversations(details, args.out)
    print(f"{len(details)} conversations, {min(args.never_reply, len(NEVER_REPLY))} who never replied → {args.out}")
    print(
        f"simulator spend: ${sum(spend for _, _, spend in conversations):.3f} (the app's model spend is in its turns)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="The app's base URL, e.g. http://localhost:8020.")
    parser.add_argument("--case", action="append", default=[], help="Only this simulated persona; repeatable.")
    parser.add_argument("--never-reply", type=int, default=len(NEVER_REPLY), help="Candidates who apply and stop.")
    parser.add_argument("--concurrency", type=int, default=4, help="Conversations at a time.")
    parser.add_argument("--simulator", default=SIMULATOR, help="Model string for the simulated candidates.")
    parser.add_argument(
        "--out",
        type=Path,
        default=RESULTS / "transcripts" / f"demo-{datetime.now(UTC):%Y%m%d-%H%M}.md",
        help="Where to write the conversations (transcripts/ is gitignored).",
    )
    asyncio.run(main(parser.parse_args()))
