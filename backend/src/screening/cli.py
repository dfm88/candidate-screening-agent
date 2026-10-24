"""Chat with the screening agent in the terminal, in memory, without a database.

    uv run --env-file .env python -m screening.cli --vacancy mx-cdmx-roma --name "José Hernández"

Type a message and press Enter. Separate messages sent together with " || " (a candidate typing in bursts).
"""

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

import logfire
from pydantic_ai import ModelResponse, TextPart, ToolCallPart

from screening.agent import build_model, history_from_turns, prompt_version, run_turn
from screening.config import DEFAULT_CLIENT_CONFIG, SERVICE_NAME, Language, Locale, Settings, load_client_config
from screening.domain import Application, ScreeningState, Status, describe_saved


async def chat(vacancy: str, name: str, language: Language, show_tools: bool) -> None:
    settings = Settings()
    logfire.configure(
        service_name=SERVICE_NAME, environment=settings.environment, send_to_logfire="if-token-present", console=False
    )
    logfire.instrument_pydantic_ai(include_content=settings.llm_content_logging)
    config = load_client_config(DEFAULT_CLIENT_CONFIG)
    application = Application(full_name=name, vacancy_location_id=vacancy, language=language)
    location = config.locations_by_id[vacancy]
    model = build_model(settings)
    conversation_id = f"cli-{uuid.uuid4().hex[:8]}"
    models = " → ".join([settings.screening_model, *settings.screening_fallback_models])
    print(f"[{conversation_id} · prompt {prompt_version(config)} · {models}]\n")

    first_message = config.render(
        "first_message", Locale.of(language, location.country), first_name=application.first_name, city=location.city
    )
    for bubble in first_message:
        print(f"{config.persona_name}> {bubble}\n")
    # Our first message is part of the conversation the agent sees, like any message we send.
    turns = [[ModelResponse(parts=[TextPart("\n\n".join(first_message))])]]
    state = ScreeningState(status=Status.INVITED, language=language)

    for line in sys.stdin:
        candidate_messages = [message.strip() for message in line.split("||") if message.strip()]
        if not candidate_messages:
            continue
        print(f"you> {' || '.join(candidate_messages)}\n")
        result = await run_turn(
            state=state,
            application=application,
            config=config,
            candidate_messages=candidate_messages,
            history=history_from_turns(turns, settings.max_history_messages),
            model=model,
            now=datetime.now(UTC),
            conversation_id=conversation_id,
        )
        state = result.state
        turns.append(result.messages)
        if show_tools:
            for part in (p for m in result.messages for p in m.parts if isinstance(p, ToolCallPart)):
                print(f"  · {part.tool_name}({part.args_as_json_str()})")
        for bubble in result.reply:
            print(f"{config.persona_name}> {bubble}\n")

    saved = "; ".join(f"{field}={value}" for field, value in describe_saved(state, config).items())
    print(f"[status={state.status} outcome={state.outcome or 'none'} saved: {saved or 'nothing'}]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vacancy", default="es-mad-chamberi", help="Location id from the client config.")
    parser.add_argument("--name", default="Lucía Martín García", help="Name on the application.")
    parser.add_argument("--language", default=Language.ES, type=Language, choices=list(Language))
    parser.add_argument("--show-tools", action="store_true", help="Print the tool calls of each turn.")
    args = parser.parse_args()
    asyncio.run(chat(args.vacancy, args.name, args.language, args.show_tools))


if __name__ == "__main__":
    main()
