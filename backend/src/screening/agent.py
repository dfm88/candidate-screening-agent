"""The two agents. The screening agent holds the conversation, with tools, steered by prompts/screening_agent.md
(on a voice turn, followed by prompts/spoken.md and prompts/live_call.md); the summary agent writes the recruiter's
caveats and notes once the screening ends (prompts/summary_agent.md).

The conversation lives in the prompt and is verified by evals. The tools hand the facts we own to domain.py:
service areas, dates, the saved state and the outcome.
"""

import asyncio
import hashlib
import html
import json
import re
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import logfire
from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    AgentRunResult,
    AgentRunResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RunContext,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from screening import domain
from screening.config import ClientConfig, Country, Language, Settings
from screening.domain import (
    Application,
    EndReason,
    FieldName,
    NewAnswers,
    Outcome,
    ScreeningState,
    Status,
    describe_saved,
    still_to_ask,
    transition,
)

PROMPTS = Path(__file__).parent / "prompts"
PROMPT = (PROMPTS / "screening_agent.md").read_text(encoding="utf-8")
# Appended to the instructions on a voice turn: what changes when it's spoken (a voice note too), and what only a live
# call needs.
SPOKEN = (PROMPTS / "spoken.md").read_text(encoding="utf-8")
LIVE_CALL = (PROMPTS / "live_call.md").read_text(encoding="utf-8")
MODEL_SETTINGS = ModelSettings(temperature=0.4, thinking=False)
SUMMARY_PROMPT = (PROMPTS / "summary_agent.md").read_text(encoding="utf-8")
SUMMARY_SETTINGS = ModelSettings(temperature=0.2, thinking=False)
# The code that writes what the models read besides the prompt: tool names, descriptions, argument schemas and results,
# and the context block. Hashed whole into the prompt version: an edit that changes none of it bumps the version too,
# but no change to what the models read can slip past it.
CODE = Path(__file__).read_text(encoding="utf-8") + Path(domain.__file__).read_text(encoding="utf-8")
LANGUAGE_NAMES = {Language.ES: "Spanish", Language.EN: "English"}


class Medium(StrEnum):
    """How the candidate talks to us in a turn: typing in the chat, or in a live voice call. The context block names
    it as the turn's channel, and a voice turn's instructions end with the voice rules."""

    TEXT = "text"
    VOICE = "voice"


@dataclass
class Deps:
    state: ScreeningState
    application: Application
    config: ClientConfig
    now: datetime
    # The hiring team's decision, final. Read from its own column before each turn; the agent never changes it.
    decision: Outcome | None = None
    medium: Medium = Medium.TEXT


screening_agent = Agent(deps_type=Deps, output_type=str, model_settings=MODEL_SETTINGS, name="screening")


def render_instructions(config: ClientConfig) -> str:
    """The prompt with the client's data: persona, FAQ and regional glossary."""
    faq = "\n".join(f"- {entry.question} {entry.answer}" for entry in config.faq)
    regional_terms = "\n".join(
        f"- {country}: " + ", ".join(row[country] for row in config.regional_terms) for country in Country
    )
    return PROMPT.format(persona=config.persona_name, client=config.client_name, faq=faq, regional_terms=regional_terms)


def render_summary_instructions(config: ClientConfig) -> str:
    return SUMMARY_PROMPT.format(client=config.client_name, language=LANGUAGE_NAMES[config.recruiter_language])


def prompt_version(config: ClientConfig, model_settings: ModelSettings = MODEL_SETTINGS) -> str:
    """Recorded on every turn and eval run. Everything both agents read apart from the conversation, and their model
    settings: a change to any of it is a new version, because the evals measure them together."""
    content = render_instructions(config) + json.dumps(model_settings, sort_keys=True)
    content += render_summary_instructions(config) + json.dumps(SUMMARY_SETTINGS, sort_keys=True)
    # The whole client config: the first message and quick-reply labels are in the history, the areas in tool results.
    content += SPOKEN + LIVE_CALL + config.model_dump_json() + CODE
    return hashlib.sha256(content.encode()).hexdigest()[:8]


@screening_agent.instructions
def instructions(ctx: RunContext[Deps]) -> str:
    """A text turn reads exactly the instructions the evals validated. A voice turn reads the same, then the voice
    rules: at the end, so the shared part stays a prefix the provider's prompt cache matches for both."""
    rendered = render_instructions(ctx.deps.config)
    if ctx.deps.medium is Medium.VOICE:
        return f"{rendered}\n{SPOKEN}\n{LIVE_CALL}"
    return rendered


@screening_agent.tool(docstring_format="google", require_parameter_descriptions=True)
def save_answers(ctx: RunContext[Deps], answers: NewAnswers) -> domain.SaveResult:
    """Save the candidate's answers, several at once and in any order, corrections included.

    Args:
        answers: Only what the candidate said explicitly.
    """
    deps = ctx.deps
    deps.state, result = domain.save_answers(
        deps.state, answers, deps.application, deps.config, deps.now, deps.decision
    )
    return result


@screening_agent.tool(docstring_format="google", require_parameter_descriptions=True)
def mark_unclear(ctx: RunContext[Deps], field: FieldName, note: str) -> str:
    """Give up on a field after asking twice without a usable answer. The recruiter sees it as unclear.

    Args:
        field: The field to give up on.
        note: What the candidate said, in a few words, e.g. "license from Venezuela, validity unknown".
    """
    state = ctx.deps.state
    if state.is_saved(field):
        return f"{field} is already saved."
    state.unclear[field] = note
    return (
        f"Recorded {field} as unclear. Still to ask: {', '.join(still_to_ask(state, ctx.deps.decision)) or 'nothing'}."
    )


@screening_agent.tool(docstring_format="google", require_parameter_descriptions=True)
def request_human(ctx: RunContext[Deps], reason: str) -> str:
    """The candidate asks for a person, or accepts your offer of one. A recruiter will review the screening.

    Args:
        reason: What the candidate wants, in a few words.
    """
    ctx.deps.state = domain.request_human(ctx.deps.state, reason)
    if ctx.deps.decision:
        decision = ctx.deps.decision
        return f"Recorded for the hiring team, who will contact the candidate. Their decision ({decision}) is final."
    return "Recorded: a person from the team will review the screening and contact the candidate."


@screening_agent.tool(docstring_format="google", require_parameter_descriptions=True)
def forward_question(ctx: RunContext[Deps], question: str) -> str:
    """Pass on a question the FAQ doesn't answer. A recruiter replies to the candidate.

    Args:
        question: The candidate's question, in their words.
    """
    ctx.deps.state.open_questions.append(question)
    return "Forwarded to the team."


@screening_agent.tool(docstring_format="google", require_parameter_descriptions=True)
def end_conversation(ctx: RunContext[Deps], reason: EndReason) -> str:
    """Stop the screening: not interested (or STOP), not the person who applied, or abuse after one warning.

    Args:
        reason: Why the conversation ends.
    """
    ctx.deps.state = domain.end_conversation(ctx.deps.state, reason)
    if reason is EndReason.CONDUCT:
        return "Ended. A person from the team will review the conversation. Say a short, polite goodbye."
    return "Ended: nobody will contact the candidate again. Say a short, polite goodbye."


@screening_agent.tool
def finish_screening(ctx: RunContext[Deps]) -> domain.FinishResult:
    """Compute the outcome: after the candidate confirms the recap, right after a knockout "no", or when they asked
    for a person and don't want to continue. Code decides the outcome; you phrase the close."""
    ctx.deps.state, result = domain.finish_screening(ctx.deps.state, ctx.deps.decision)
    return result


def model_from_string(model: str, settings: Settings) -> Model:
    """A model from its PydanticAI model string, e.g. `google-cloud:gemini-3.8-flash` or `openai:gpt-6-luna`."""
    provider, _, name = model.partition(":")
    if provider == "google-cloud":
        # The provider is explicit so the location always applies. From a plain string, GoogleCloudProvider falls
        # back to us-central1 without GOOGLE_CLOUD_LOCATION, and with GOOGLE_API_KEY in the env it switches to
        # Express Mode on that key and ignores the location. An explicit location makes it use ADC instead.
        cloud = GoogleCloudProvider(project=settings.google_cloud_project, location=settings.google_cloud_location)
        return GoogleModel(name, provider=cloud)
    return infer_model(model)


def build_model(settings: Settings) -> Model:
    """The screening model, with its fallbacks tried in order when it fails."""
    primary = model_from_string(settings.screening_model, settings)
    fallbacks = [model_from_string(model, settings) for model in settings.screening_fallback_models]
    return FallbackModel(primary, *fallbacks) if fallbacks else primary


def render_context(
    state: ScreeningState,
    application: Application,
    config: ClientConfig,
    now: datetime,
    decision: Outcome | None = None,
    medium: Medium = Medium.TEXT,
) -> str:
    """The <system_ctx> block: values only (the clock, the country, the state). The rules are in the prompt. Once the
    hiring team has decided, their decision is the outcome; the rules' outcome is for their page only."""
    location = config.locations_by_id[application.vacancy_location_id]
    saved = describe_saved(state, config)
    outcome = f"{decision} (the hiring team's decision, final)" if decision else state.outcome or "none"
    lines = [
        f"now: {now.astimezone(location.tz):%Y-%m-%d %H:%M (%A)} {location.timezone}",
        f"country_of_work: {location.country}",
        f"default_language: {application.language}",
        f"channel: {medium}",
        f"vacancy: {location.name}",
        f"name_on_application: {application.full_name}",
        f"status: {state.status}",
        f"outcome: {outcome}",
        "saved: " + ("; ".join(f"{field}={value}" for field, value in saved.items()) or "nothing yet"),
        "unclear: " + ("; ".join(f"{field} ({note})" for field, note in state.unclear.items()) or "none"),
        "still_to_ask: " + (", ".join(still_to_ask(state, decision)) or "nothing"),
        "review_flags: " + (", ".join(flag.reason for flag in state.flags) or "none"),
        # With these, every tool's effect is in the block: the model can tell what has been done from it alone.
        "open_questions: " + ("; ".join(state.open_questions) or "none"),
        "talent_pool_opt_in: " + {True: "yes", False: "no", None: "none"}[state.talent_pool_opt_in],
    ]
    # Names and places come from candidates: escaped, so no value can close the block or open a new one.
    return "<system_ctx>\n" + html.escape("\n".join(lines), quote=False) + "\n</system_ctx>"


def candidate_messages_block(candidate_messages: Sequence[str]) -> str:
    """What the candidate said in a turn. Their text is escaped so it can't pose as a <system_ctx> block."""
    escaped = [html.escape(message, quote=False) for message in candidate_messages]
    return "<new_messages>\n" + "".join(f"<message>{message}</message>\n" for message in escaped) + "</new_messages>"


def call_history(lines: Sequence[tuple[str, str]]) -> list[ModelMessage]:
    """A live voice call so far, from ElevenLabs' transcript as (role, content) lines: the candidate's words as
    transcribed ("user"), and ours ("agent") cut to what was played. Built from it on every voice turn, so an answer
    is only ever read against a question the candidate heard. The lines carry no context block: the turn's own is
    the current one."""
    history: list[ModelMessage] = []
    for role, content in lines:
        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(candidate_messages_block([content]))]))
        elif content.strip():
            history.append(ModelResponse(parts=[TextPart(content)]))
    return history


def history_from_turns(turns: list[list[ModelMessage]], max_messages: int) -> list[ModelMessage]:
    """The most recent whole turns that fit in `max_messages`: a tool call never loses its result."""
    kept: list[list[ModelMessage]] = []
    count = 0
    for turn in reversed(turns):
        if count + len(turn) > max_messages:
            break
        kept.insert(0, turn)
        count += len(turn)
    return [message for turn in kept for message in turn]


@dataclass
class TurnResult:
    reply: list[str]
    state: ScreeningState
    # This turn's messages, stored exactly as sent: the history stays append-only, so the provider's prompt cache
    # keeps matching it. Each turn keeps its own <system_ctx> snapshot; the prompt says only the latest is current.
    messages: list[ModelMessage]
    usage: RunUsage


@contextmanager
def start_turn(
    *,
    state: ScreeningState,
    application: Application,
    config: ClientConfig,
    candidate_messages: list[str],
    now: datetime,
    conversation_id: str,
    decision: Outcome | None,
    medium: Medium,
) -> Generator[tuple[str, Deps]]:
    """What a turn runs on, whichever way it runs: the prompt (the context block, then the candidate's words) and the
    deps the tools change, inside the turn's span."""
    state = state.model_copy(deep=True)
    status_before = state.status
    # A candidate who writes is in the screening, even after going silent.
    if state.status in (Status.PENDING, Status.INVITED, Status.ABANDONED):
        state.status = transition(state.status, Status.IN_PROGRESS)

    context = render_context(state, application, config, now, decision, medium)
    prompt = f"{context}\n{candidate_messages_block(candidate_messages)}"
    deps = Deps(state=state, application=application, config=config, now=now, decision=decision, medium=medium)
    cut_off: asyncio.CancelledError | GeneratorExit | None = None
    with logfire.span(
        "screening turn {conversation_id}",
        conversation_id=conversation_id,
        prompt_version=prompt_version(config),
        status_before=status_before,
        decision=decision or "none",
    ) as span:
        try:
            yield prompt, deps
        except (asyncio.CancelledError, GeneratorExit) as stopped:
            # A call drops its turn when the candidate speaks again or hangs up: that's how a call goes, not an error.
            # Raised again outside the span, which would record anything that leaves it as an error.
            cut_off = stopped
            span.message = f"{span.message}: cut off"
            span.set_attribute("cut_off", True)
        else:
            span.set_attributes({"status_after": deps.state.status, "outcome": deps.state.outcome or "none"})
    if cut_off is not None:
        raise cut_off


def turn_result(result: AgentRunResult[str], deps: Deps) -> TurnResult:
    messages = result.new_messages()
    if deps.medium is Medium.VOICE:
        # One spoken turn, as it streamed: the text written before a tool call is said too, not only the output.
        spoken = " ".join(
            part.content.strip()
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
            if isinstance(part, TextPart) and part.content.strip()
        )
        reply = [spoken] if spoken else []
    else:
        reply = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", result.output) if paragraph.strip()]
    return TurnResult(reply=reply, state=deps.state, messages=messages, usage=result.usage)


async def run_turn(
    *,
    state: ScreeningState,
    application: Application,
    config: ClientConfig,
    candidate_messages: list[str],
    history: list[ModelMessage],
    model: Model,
    now: datetime,
    conversation_id: str,
    model_settings: ModelSettings | None = None,
    turn_timeout: float | None = None,
    usage_limits: UsageLimits | None = None,
    decision: Outcome | None = None,
    medium: Medium = Medium.TEXT,
) -> TurnResult:
    """One turn, answered whole. The operational bounds (`model_settings`, `turn_timeout`, `usage_limits`) go to
    `agent.run` as they are: its settings merge over MODEL_SETTINGS, so they stay out of the prompt version, and its
    own timeout ends the run with RunTimedOut rather than a bare cancellation."""
    with start_turn(
        state=state,
        application=application,
        config=config,
        candidate_messages=candidate_messages,
        now=now,
        conversation_id=conversation_id,
        decision=decision,
        medium=medium,
    ) as (prompt, deps):
        result = await screening_agent.run(
            prompt,
            message_history=history,
            deps=deps,
            model=model,
            conversation_id=conversation_id,
            model_settings=model_settings,
            timeout=turn_timeout,
            usage_limits=usage_limits,
        )
    return turn_result(result, deps)


async def stream_turn(
    *,
    state: ScreeningState,
    application: Application,
    config: ClientConfig,
    candidate_messages: list[str],
    history: list[ModelMessage],
    model: Model,
    now: datetime,
    conversation_id: str,
    model_settings: ModelSettings | None = None,
    turn_timeout: float | None = None,
    usage_limits: UsageLimits | None = None,
    decision: Outcome | None = None,
) -> AsyncGenerator[str | TurnResult]:
    """A turn of a live call: its text as it's written, to be spoken, then its result. The same turn as `run_turn`,
    through `agent.run_stream_events`, so a call that stops reading it (the candidate spoke again, or hung up) cancels
    the run when the block exits, and the turn leaves nothing behind."""
    result: AgentRunResult[str] | None = None
    with start_turn(
        state=state,
        application=application,
        config=config,
        candidate_messages=candidate_messages,
        now=now,
        conversation_id=conversation_id,
        decision=decision,
        medium=Medium.VOICE,
    ) as (prompt, deps):
        async with screening_agent.run_stream_events(
            prompt,
            message_history=history,
            deps=deps,
            model=model,
            conversation_id=conversation_id,
            model_settings=model_settings,
            timeout=turn_timeout,
            usage_limits=usage_limits,
        ) as events:
            async for event in events:
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    yield event.part.content
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    yield event.delta.content_delta
                elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                    # The turn's parts are spoken as one text: the one after a tool call starts a new word.
                    yield " "
                elif isinstance(event, AgentRunResultEvent):
                    result = event.result
    # The events end with the result, or raise.
    assert result is not None
    yield turn_result(result, deps)


# --- The recruiter summary -------------------------------------------------------------------------------------


class RecruiterSummary(BaseModel):
    """Shown as it is at the top of the recruiter's detail page, under the key data."""

    caveats: list[str] = Field(
        max_length=3, description="What the recruiter should check before deciding, in plain words. One line each."
    )
    notes: list[str] = Field(
        max_length=3, description="Worth knowing from the conversation, and not in the answers. One line each."
    )


summary_agent = Agent(
    deps_type=ClientConfig, output_type=RecruiterSummary, model_settings=SUMMARY_SETTINGS, name="summary"
)


@summary_agent.instructions
def summary_instructions(ctx: RunContext[ClientConfig]) -> str:
    return render_summary_instructions(ctx.deps)


async def write_summary(
    state: ScreeningState,
    application: Application,
    config: ClientConfig,
    transcript: str,
    model: Model,
    now: datetime,
    conversation_id: str,
    model_settings: ModelSettings | None = None,
) -> RecruiterSummary:
    """Runs once when the screening ends. The key data come from code; the summary adds what they don't hold."""
    data = domain.key_data(state, config)
    location = config.locations_by_id[application.vacancy_location_id]
    # Without today's date, a start date of today next to "I can start right away" looks like a discrepancy.
    lines = [f"today: {now.astimezone(location.tz):%Y-%m-%d (%A)}"]
    lines += [f"{field}: {answer}" for field, answer in data.answers.items()]
    lines += [
        f"status: {state.status}",
        f"outcome: {data.outcome or 'none'}",
        "reasons: " + ("; ".join(data.reasons) or "none"),
        "open questions: " + ("; ".join(data.open_questions) or "none"),
    ]
    # Both blocks hold candidate text: escaped, so nothing in them can close a block or open a new one.
    screening = html.escape("\n".join(lines), quote=False)
    prompt = (
        f"<screening>\n{screening}\n</screening>\n<transcript>\n{html.escape(transcript, quote=False)}\n</transcript>"
    )
    with logfire.span(
        "screening summary {conversation_id}", conversation_id=conversation_id, prompt_version=prompt_version(config)
    ):
        result = await summary_agent.run(prompt, deps=config, model=model, model_settings=model_settings)
    return result.output
