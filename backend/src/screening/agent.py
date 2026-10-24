"""The screening agent: one conversational agent with tools, steered by prompts/screening_agent.md.

The conversation lives in the prompt and is verified by evals. The tools hand the facts we own to domain.py:
service areas, dates, the saved state and the outcome.
"""

import hashlib
import html
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import logfire
from anthropic import AsyncAnthropicVertex
from pydantic_ai import Agent, ModelMessage, ModelRequest, RunContext, UserPromptPart
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from screening import domain
from screening.config import ClientConfig, Settings
from screening.domain import (
    Application,
    EndReason,
    FieldName,
    NewAnswers,
    Outcome,
    ReviewFlag,
    ReviewReason,
    ScreeningState,
    Status,
    describe_saved,
    transition,
)

PROMPT = (Path(__file__).parent / "prompts" / "screening_agent.md").read_text(encoding="utf-8")
MODEL_SETTINGS = ModelSettings(temperature=0.4, thinking=False)
# Recorded on every turn and eval run: a change to the prompt or to the settings is a new version.
PROMPT_VERSION = hashlib.sha256((PROMPT + json.dumps(MODEL_SETTINGS, sort_keys=True)).encode()).hexdigest()[:8]


@dataclass
class Deps:
    state: ScreeningState
    application: Application
    config: ClientConfig
    now: datetime


screening_agent = Agent(deps_type=Deps, output_type=str, model_settings=MODEL_SETTINGS, name="screening")


@screening_agent.instructions
def instructions(ctx: RunContext[Deps]) -> str:
    config = ctx.deps.config
    faq = "\n".join(f"- {entry.question} {entry.answer}" for entry in config.faq)
    return PROMPT.format(persona=config.persona_name, client=config.client_name, faq=faq)


@screening_agent.tool(docstring_format="google", require_parameter_descriptions=True)
def save_answers(ctx: RunContext[Deps], answers: NewAnswers) -> domain.SaveResult:
    """Save the candidate's answers, several at once and in any order, corrections included.

    Args:
        answers: Only what the candidate said explicitly.
    """
    deps = ctx.deps
    deps.state, result = domain.save_answers(deps.state, answers, deps.application, deps.config, deps.now)
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
    return f"Recorded {field} as unclear. Still to ask: {', '.join(state.still_to_ask) or 'nothing'}."


@screening_agent.tool(docstring_format="google", require_parameter_descriptions=True)
def request_human(ctx: RunContext[Deps], reason: str) -> str:
    """The candidate asks for a person, or accepts your offer of one. A recruiter will review the screening.

    Args:
        reason: What the candidate wants, in a few words.
    """
    state = ctx.deps.state
    state.flags.append(ReviewFlag(reason=ReviewReason.HUMAN_REQUESTED, detail=reason))
    if state.status is Status.COMPLETED:
        # After the close, asking for a person moves any outcome to review (process design: outcomes).
        state.outcome = Outcome.NEEDS_HUMAN_REVIEW
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
    state = ctx.deps.state
    if reason is EndReason.CONDUCT:
        state.flags.append(ReviewFlag(reason=ReviewReason.CONDUCT))
        if state.status is not Status.COMPLETED:
            state.status = transition(state.status, Status.COMPLETED)
        state.outcome = Outcome.NEEDS_HUMAN_REVIEW
        return "Ended. A person from the team will review the conversation. Say a short, polite goodbye."
    if state.status is not Status.WITHDRAWN:
        state.status = transition(state.status, Status.WITHDRAWN)
    return "Ended: nobody will contact the candidate again. Say a short, polite goodbye."


@screening_agent.tool
def finish_screening(ctx: RunContext[Deps]) -> domain.FinishResult:
    """Compute the outcome: after the candidate confirms the recap, right after a knockout "no", or when they asked
    for a person and don't want to continue. Code decides the outcome; you phrase the close."""
    ctx.deps.state, result = domain.finish_screening(ctx.deps.state)
    return result


def build_model(settings: Settings) -> Model:
    """The primary model and its fallbacks, from comma-separated specs in settings."""
    models: list[Model] = []
    for spec in settings.screening_models.split(","):
        provider, _, name = spec.strip().partition(":")
        # Candidate data stays in the EU: Vertex models always get the configured location explicitly, never
        # the provider's us-central1 default.
        if provider == "google-cloud":
            location = settings.vertex_location
            models.append(
                GoogleModel(
                    name, provider=GoogleCloudProvider(project=settings.google_cloud_project, location=location)
                )
            )
        elif provider == "anthropic-vertex":
            # PydanticAI has no model string for Claude on Vertex AI, so the Anthropic client is built here.
            if not settings.google_cloud_project:
                raise ValueError("anthropic-vertex models need GOOGLE_CLOUD_PROJECT")
            client = AsyncAnthropicVertex(region=settings.vertex_location, project_id=settings.google_cloud_project)
            models.append(AnthropicModel(name, provider=AnthropicProvider(anthropic_client=client)))
        else:
            models.append(infer_model(spec.strip()))
    return models[0] if len(models) == 1 else FallbackModel(*models)


def render_context(state: ScreeningState, application: Application, config: ClientConfig, now: datetime) -> str:
    """The <system_ctx> block: values only (the clock, the country, the state). The rules are in the prompt."""
    location = config.locations_by_id[application.vacancy_location_id]
    saved = describe_saved(state, config)
    lines = [
        f"now: {now.astimezone(location.tz):%Y-%m-%d %H:%M (%A)} {location.timezone}",
        f"country_of_work: {location.country}",
        f"default_language: {application.language}",
        f"vacancy: {location.name}",
        f"name_on_application: {application.full_name}",
        f"status: {state.status}",
        f"outcome: {state.outcome or 'none'}",
        "saved: " + ("; ".join(f"{field}={value}" for field, value in saved.items()) or "nothing yet"),
        "unclear: " + ("; ".join(f"{field} ({note})" for field, note in state.unclear.items()) or "none"),
        "still_to_ask: " + (", ".join(state.still_to_ask) or "nothing"),
        "review_flags: " + (", ".join(flag.reason for flag in state.flags) or "none"),
    ]
    # Names and places come from candidates: escaped, so no value can close the block or open a new one.
    return "<system_ctx>\n" + html.escape("\n".join(lines), quote=False) + "\n</system_ctx>"


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
    # This turn's messages to store, the context block replaced by the candidate's words.
    messages: list[ModelMessage]
    usage: RunUsage


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
) -> TurnResult:
    state = state.model_copy(deep=True)
    status_before = state.status
    # A candidate who writes is in the screening, even after going silent.
    if state.status in (Status.PENDING, Status.INVITED, Status.ABANDONED):
        state.status = transition(state.status, Status.IN_PROGRESS)

    # Candidate text is escaped so it can't pose as a <system_ctx> block.
    escaped = [html.escape(message, quote=False) for message in candidate_messages]
    new_messages = "".join(f"<message>{message}</message>\n" for message in escaped)
    prompt = f"{render_context(state, application, config, now)}\n<new_messages>\n{new_messages}</new_messages>"

    deps = Deps(state=state, application=application, config=config, now=now)
    with logfire.span(
        "screening turn {conversation_id}",
        conversation_id=conversation_id,
        prompt_version=PROMPT_VERSION,
        status_before=status_before,
    ) as span:
        result = await screening_agent.run(
            prompt, message_history=history, deps=deps, model=model, conversation_id=conversation_id
        )
        span.set_attributes({"status_after": deps.state.status, "outcome": deps.state.outcome or "none"})

    # Only the current message carries the context block. Stored turns keep the candidate's words alone, so
    # the history never replays a stale clock or state as current. Logfire keeps what was actually sent.
    candidate_text = "\n".join(escaped)
    stored = [
        replace(
            message,
            parts=[
                replace(part, content=candidate_text) if isinstance(part, UserPromptPart) else part
                for part in message.parts
            ],
        )
        if isinstance(message, ModelRequest)
        else message
        for message in result.new_messages()
    ]
    reply = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", result.output) if paragraph.strip()]
    return TurnResult(reply=reply, state=deps.state, messages=stored, usage=result.usage)
