"""The eval task: a candidate chats with the screening agent, in memory, until the conversation ends; then the
summary agent writes the recruiter summary.

The candidate is simulated: an LLM plays a persona from cases.yaml and, before each reply, labels what the agent's
latest messages asked for. The deterministic evaluators read those labels (knockouts first, no re-asks, at most two
tries). A scripted case replays fixed candidate messages instead, to reproduce an observed failure exactly.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelMessage, ModelRequest, ModelResponse, RunContext, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_evals import increment_eval_metric

from screening.agent import RecruiterSummary, history_from_turns, run_turn, write_summary
from screening.config import DEFAULT_CLIENT_CONFIG, Language, Locale, load_client_config
from screening.domain import (
    Application,
    Availability,
    FieldName,
    LicenseAnswer,
    Outcome,
    ReviewReason,
    Schedule,
    ScreeningState,
    Status,
)

CONFIG = load_client_config(DEFAULT_CLIENT_CONFIG)

# --- The dataset ---------------------------------------------------------------------------------------------


class Persona(BaseModel):
    """The case inputs: the application, the clock, and who the candidate is (for the simulator, in English)."""

    vacancy: str
    name_on_application: str
    application_language: Language
    now: datetime
    profile: str
    max_turns: int = 14
    # Fixed candidate messages, one list per turn, sent instead of the simulator's: a replayed failure.
    script: list[list[str]] | None = None


class ExpectedAnswers(BaseModel):
    """Only the fields a case sets are checked. location_id null means: outside every service area."""

    full_name: str | None = None
    license: LicenseAnswer | None = None
    location_id: str | None = None
    availability: list[Availability] | None = None
    schedule: list[Schedule] | None = None
    experience_years: float | None = None
    platforms: list[str] | None = None
    start_date: date | None = None


class Expected(BaseModel):
    """The case metadata: what the screening should end with."""

    tags: list[str] = []
    status: Status
    outcome: Outcome | None = None
    review_reasons: list[ReviewReason] = []
    unclear: list[FieldName] = []
    answers: ExpectedAnswers = ExpectedAnswers()
    final_language: Language
    talent_pool_opt_in: bool | None = None


# --- The task output -----------------------------------------------------------------------------------------

Asked = FieldName | Literal["recap", "talent_pool", "other"]


class Turn(BaseModel):
    candidate: list[str]
    reply: list[str]
    # The simulator's reading of `reply`: what it asked the candidate for.
    asked_about: list[Asked] = []
    tool_calls: list[str]
    # model_name of every response in the turn: with a fallback, this shows which model answered.
    models: list[str]
    seconds: float
    # Each model request's latency: the turn's own time minus the tools and the agent loop.
    request_seconds: list[float] = []
    # The state after the turn, i.e. when the reply was sent.
    state: ScreeningState
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float | None


class ScreeningRun(BaseModel):
    first_message: list[str]
    turns: list[Turn]
    state: ScreeningState
    # Written once the screening ends (completed or withdrawn).
    summary: RecruiterSummary | None = None

    def transcript(self, assistant: str = "Assistant", candidate: str = "Candidate") -> str:
        lines = [f"{assistant}: {message}" for message in self.first_message]
        for turn in self.turns:
            lines += [f"{candidate}: {message}" for message in turn.candidate]
            lines += [f"{assistant}: {message}" for message in turn.reply]
        return "\n".join(lines)


# --- The simulated candidate ---------------------------------------------------------------------------------


class CandidateTurn(BaseModel):
    asked_about: list[Asked] = Field(
        description="What the assistant's latest messages ask you to provide or answer: the fields, 'recap' when "
        "it asks you to confirm things you already said, 'talent_pool' when it asks whether you want to hear about "
        "other roles, 'other' for any other question. Empty if they ask nothing."
    )
    messages: list[str] = Field(
        description="Your next chat messages, as you'd type them on your phone: usually one, up to three. "
        "Empty when done."
    )
    done: bool = Field(
        description="True when the conversation is over for you: the assistant said goodbye or gave you the "
        "result and asked nothing more, or you have nothing left to say."
    )


simulator = Agent(deps_type=Persona, output_type=CandidateTurn, name="candidate_simulator")


@simulator.instructions
def simulator_instructions(ctx: RunContext[Persona]) -> str:
    persona = ctx.deps
    return f"""You are role-playing a job applicant in a test of a recruiting chat assistant. You applied for a
delivery driver job and the assistant now screens you by chat. Today is {persona.now:%A %d %B %Y}.

Who you are and how you behave:
{persona.profile}

Rules:
- Stay in character. Use only the facts above; if asked something they don't cover, answer briefly and
  plausibly, and stay consistent.
- Write like a real person on a phone: short, informal, no lists. Don't volunteer everything at once unless
  your profile says so. Answer what you're asked.
- Never mention that this is a test or a role-play."""


def candidate_prompt(first_message: list[str], turns: list[Turn]) -> str:
    run = ScreeningRun(first_message=first_message, turns=turns, state=ScreeningState(language=Language.ES))
    return (
        f"<conversation>\n{run.transcript(candidate='You')}\n</conversation>\n"
        "Label what the assistant's latest messages ask you, then write your next messages."
    )


# --- The task ------------------------------------------------------------------------------------------------

SIMULATOR_SETTINGS = ModelSettings(temperature=0.7, thinking=False)


def response_cost(response: ModelResponse) -> float | None:
    """The price from genai-prices, as PydanticAI computes it; None for a model it has no price for."""
    try:
        return float(response.cost().total_price)
    except LookupError:
        return None


def make_task(
    model: Model, simulator_model: Model, max_history_messages: int
) -> Callable[[Persona], Awaitable[ScreeningRun]]:
    async def screen(persona: Persona) -> ScreeningRun:
        application = Application(
            full_name=persona.name_on_application,
            vacancy_location_id=persona.vacancy,
            language=persona.application_language,
        )
        location = CONFIG.locations_by_id[persona.vacancy]
        first_message = CONFIG.render(
            "first_message",
            Locale.of(application.language, location.country),
            first_name=application.first_name,
            city=location.city,
        )
        stored_turns: list[list[ModelMessage]] = [[ModelResponse(parts=[TextPart("\n\n".join(first_message))])]]
        state = ScreeningState(status=Status.INVITED, language=application.language)
        conversation_id = f"eval-{uuid.uuid4().hex[:8]}"
        turns: list[Turn] = []

        for number in range(len(persona.script) if persona.script else persona.max_turns):
            if persona.script:
                messages, done = persona.script[number], False
            else:
                candidate = await simulator.run(
                    candidate_prompt(first_message, turns),
                    deps=persona,
                    model=simulator_model,
                    model_settings=SIMULATOR_SETTINGS,
                )
                increment_eval_metric(
                    "simulator_cost_usd",
                    sum(response_cost(m) or 0 for m in candidate.new_messages() if isinstance(m, ModelResponse)),
                )
                if turns:
                    turns[-1].asked_about = candidate.output.asked_about
                messages, done = candidate.output.messages, candidate.output.done
            # A last message ("no, gracias") still goes out before the candidate leaves.
            if not messages:
                break

            started = time.perf_counter()
            result = await run_turn(
                state=state,
                application=application,
                config=CONFIG,
                candidate_messages=messages,
                history=history_from_turns(stored_turns, max_history_messages),
                model=model,
                now=persona.now,
                conversation_id=conversation_id,
            )
            seconds = time.perf_counter() - started
            state = result.state
            stored_turns.append(result.messages)

            responses = [message for message in result.messages if isinstance(message, ModelResponse)]
            costs = [response_cost(response) for response in responses]
            # PydanticAI stamps a request when it's sent and a response when it arrives, both on the local clock.
            sent_at = [message.timestamp for message in result.messages if isinstance(message, ModelRequest)]
            request_seconds = [
                (response.timestamp - sent).total_seconds()
                for sent, response in zip(sent_at, responses, strict=True)
                if sent is not None
            ]
            turn = Turn(
                candidate=messages,
                reply=result.reply,
                tool_calls=[part.tool_name for r in responses for part in r.parts if isinstance(part, ToolCallPart)],
                models=[response.model_name or "unknown" for response in responses],
                seconds=seconds,
                request_seconds=request_seconds,
                state=state,
                input_tokens=result.usage.input_tokens,
                cache_read_tokens=result.usage.cache_read_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=None if None in costs else sum(cost or 0 for cost in costs),
            )
            turns.append(turn)
            increment_eval_metric("turns", 1)
            increment_eval_metric("agent_input_tokens", turn.input_tokens)
            increment_eval_metric("agent_cache_read_tokens", turn.cache_read_tokens)
            increment_eval_metric("agent_cost_usd", turn.cost_usd or 0)
            if done:
                break

        run = ScreeningRun(first_message=first_message, turns=turns, state=state)
        if state.status in (Status.COMPLETED, Status.WITHDRAWN):
            run.summary = await write_summary(
                state, application, CONFIG, run.transcript(), model, persona.now, conversation_id
            )
        return run

    return screen
