"""The eval task: a candidate chats with the screening agent, in memory, until the conversation ends; then the
summary agent writes the recruiter summary.

The candidate is simulated: an LLM plays a persona from cases.yaml and, with each reply, labels what the agent's
latest messages asked for and what its own messages provide. The deterministic evaluators read those labels (knockouts
first, no re-asks, at most two tries, nothing saved that wasn't provided). A scripted case replays fixed candidate
messages instead, to reproduce an observed failure exactly, and declares what each of them provides.
"""

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelMessage, ModelRequest, ModelResponse, RunContext, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_evals import increment_eval_metric

from screening.agent import Medium, RecruiterSummary, history_from_turns, run_turn, save_answers, write_summary
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
    describe_saved,
)
from screening.service import render_reminder

CONFIG = load_client_config(DEFAULT_CLIENT_CONFIG)

# --- The dataset ---------------------------------------------------------------------------------------------


class ScriptedDecision(BaseModel):
    """A recruiter's decision, landing between two scripted turns as it does in the service: a template in the chat."""

    after_turn: int = Field(ge=1, description="The decision lands after this many scripted turns.")
    outcome: Literal[Outcome.QUALIFIED, Outcome.NOT_QUALIFIED]


class ScriptedReminder(BaseModel):
    """The mid-chat reminder, sent after a scripted turn as the clock sends it after a silence: a template in the chat.
    The case's clock doesn't move: no rule the agent follows depends on the hours in between."""

    after_turn: int = Field(ge=1, description="The candidate goes quiet after this many scripted turns.")


class ScriptedTurn(BaseModel):
    """One turn of a replayed exchange: the candidate's messages and the fields they give an answer for."""

    messages: list[str]
    provides: list[FieldName] = []


class Persona(BaseModel):
    """The case inputs: the application, the clock, and who the candidate is (for the simulator, in English)."""

    vacancy: str
    name_on_application: str
    application_language: Language
    now: datetime
    profile: str
    max_turns: int = 14
    # Voice: the turns read the voice rules too and are spoken as one reply, as in a call. The history is still the
    # stored turns, tool calls included, not the call's transcript: no cut-offs, no speech-to-text.
    medium: Medium = Medium.TEXT
    # Fixed candidate messages, sent instead of the simulator's: a replayed failure.
    script: list[ScriptedTurn] | None = None
    decision: ScriptedDecision | None = None
    reminder: ScriptedReminder | None = None


class ExpectedAnswers(BaseModel):
    """Only the fields a case sets are checked. location_id null means: outside every service area."""

    full_name: str | None = None
    license: LicenseAnswer | None = None
    location_id: str | None = None
    availability: list[Availability] | None = None
    schedule: list[Schedule] | None = None
    experience_years: float | None = None
    platforms: list[str] | None = None
    # A list when more than one is right; null in it accepts no date saved (the agent asked instead).
    start_date: date | list[date | None] | None = None


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
    # The fields `candidate` gives an answer for: the simulator's label, or the script's.
    provided: list[FieldName] = []
    reply: list[str]
    # Templates we sent after the reply, before the candidate's next message: the recruiter's decision, a reminder.
    sent_after: list[str] = []
    reminder: list[str] = []
    # The simulator's reading of `reply`: what it asked the candidate for.
    asked_about: list[Asked] = []
    tool_calls: list[str]
    # The arguments of each save_answers call: tool_calls holds names only, and the judge reads what was passed.
    save_arguments: list[dict[str, Any]] = []
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

    def transcript(self, assistant: str = "Assistant", candidate: str = "Candidate", with_saves: bool = False) -> str:
        """With saves, each candidate turn is followed by what the assistant saved in it and the arguments it passed."""
        lines = [f"{assistant}: {message}" for message in self.first_message]
        for turn, saved in zip(self.turns, self.saved_by_turn(), strict=True):
            lines += [f"{candidate}: {message}" for message in turn.candidate]
            if with_saves and saved:
                values = "; ".join(f"{field}={value}" for field, value in saved.items())
                arguments = " ".join(json.dumps(arguments, ensure_ascii=False) for arguments in turn.save_arguments)
                lines.append(f"Saved: {values} (arguments: {arguments})")
            lines += [f"{assistant}: {message}" for message in turn.reply]
            # Labelled apart, so the judge can tell the hiring team's decision from the assistant's replies.
            lines += [f"Hiring team (decision): {message}" for message in turn.sent_after]
            lines += [f"{assistant} (reminder, after a silence): {message}" for message in turn.reminder]
        return "\n".join(lines)

    def saved_by_turn(self) -> list[dict[FieldName, str]]:
        """What each turn saved, as the recruiter reads it: the fields whose value differs from the turn before."""
        saved: list[dict[FieldName, str]] = []
        before: dict[FieldName, str] = {}
        for turn in self.turns:
            after = describe_saved(turn.state, CONFIG)
            saved.append({field: value for field, value in after.items() if before.get(field) != value})
            before = after
        return saved


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
    provided: list[FieldName] = Field(
        description="The fields your new messages give an answer for, asked or not, corrections included. A yes to "
        "'is your full name …?' gives full_name; a yes to a recap gives nothing new. A yes that only agrees to start "
        "or to go on, a greeting or a question gives nothing. Empty if they give none."
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
        decision: Outcome | None = None

        for number in range(len(persona.script) if persona.script else persona.max_turns):
            if persona.script:
                messages, provided, done = persona.script[number].messages, persona.script[number].provides, False
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
                messages, provided, done = candidate.output.messages, candidate.output.provided, candidate.output.done
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
                decision=decision,
                medium=persona.medium,
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
            calls = [part for response in responses for part in response.parts if isinstance(part, ToolCallPart)]
            turn = Turn(
                candidate=messages,
                provided=provided,
                reply=result.reply,
                tool_calls=[call.tool_name for call in calls],
                save_arguments=[call.args_as_dict() for call in calls if call.tool_name == save_answers.__name__],
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
            if persona.decision and persona.decision.after_turn == len(turns):
                # Stored as the service stores it: a turn of its own in the history, and a message in the chat.
                if state.status is not Status.COMPLETED:
                    raise ValueError(f"the decision lands after turn {len(turns)}, but the screening hasn't finished")
                turn.sent_after = CONFIG.render(
                    f"decision_{persona.decision.outcome}",
                    Locale.of(state.language, location.country),
                    first_name=application.first_name,
                    city=location.city,
                )
                stored_turns.append([ModelResponse(parts=[TextPart("\n\n".join(turn.sent_after))])])
                decision = persona.decision.outcome
            if persona.reminder and persona.reminder.after_turn == len(turns):
                if state.status is not Status.IN_PROGRESS:
                    raise ValueError(f"the reminder comes after turn {len(turns)}, but the screening isn't in progress")
                turn.reminder = render_reminder(CONFIG, "reminder_in_progress", state, application)
                stored_turns.append([ModelResponse(parts=[TextPart("\n\n".join(turn.reminder))])])
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
