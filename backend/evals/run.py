"""Run the eval dataset on model configs and print the arena: one row per config.

    uv run --group evals --env-file .env python -m evals.run --model google-cloud:gemini-3.8-flash
    uv run --group evals --env-file .env python -m evals.run --model google-cloud:gemini-3.8-flash \\
        --fallback openai:gpt-6-luna                                   # the demo config
    uv run --group evals --env-file .env python -m evals.run --model A --model B --repeat 2   # the arena

Each --model is one experiment and one row; --fallback and --settings apply to every --model. Without
--fallback a row measures exactly one model. Unset ANTHROPIC_BASE_URL first if your shell sets it.
"""

import argparse
import asyncio
import json
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path

import logfire
from pydantic_ai.exceptions import ModelHTTPError, ModelRateLimitError, ModelUnavailableError
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.retries import RetryConfig, wait_retry_after
from pydantic_ai.settings import ModelSettings
from pydantic_evals import Dataset
from pydantic_evals.reporting import EvaluationReport
from pydantic_evals.reporting.analyses import ScalarResult, TableResult
from tenacity import retry_if_exception, stop_after_attempt, wait_exponential

from evals.evaluators import (
    RUBRICS,
    ArenaNumbers,
    Asking,
    CacheHealth,
    ConversationJudge,
    MessageShape,
    NoStandaloneAcknowledgement,
    OutcomeMatches,
    SavedAnswers,
    SavedLanguage,
    SpanishWording,
)
from evals.task import CONFIG, Expected, Persona, ScreeningRun, make_task
from screening.agent import MODEL_SETTINGS, build_model, model_from_string, prompt_version, screening_agent
from screening.config import SERVICE_NAME, Settings

CASES = Path(__file__).parent / "cases.yaml"
RESULTS = Path(__file__).parent / "results"
SIMULATOR = "openai:gpt-6-sol"
# The judge is an OpenAI model, so its scores on OpenAI rows may favour their style (marked in the table).
JUDGE = "openai:gpt-6-luna"
JUDGE_SETTINGS = ModelSettings(thinking="low")
ASSERTIONS = [
    "outcome",
    "knockouts_first",
    "no_reask",
    "two_tries_max",
    "no_standalone_ack",
    "saved_language",
    "regional_terms",
    "gender_neutral",
]
SCORES = ["saved_answers", "message_shape"]
JUDGE_SCORES = [*RUBRICS, "summary"]


def is_transient(error: BaseException) -> bool:
    return isinstance(error, ModelRateLimitError | ModelUnavailableError) or (
        isinstance(error, ModelHTTPError) and error.status_code == 429
    )


# A rate limit or an outage is retried with backoff, never handed to another model: a fallback would mix two
# models into one row. A retry reruns the whole conversation, from a fresh task run.
RETRY = RetryConfig(
    retry=retry_if_exception(is_transient),
    wait=wait_retry_after(wait_exponential(multiplier=2, min=5, max=60), max_wait=120),
    stop=stop_after_attempt(4),
    reraise=True,
)


def load_dataset(case_names: list[str]) -> Dataset[Persona, ScreeningRun, Expected]:
    dataset = Dataset[Persona, ScreeningRun, Expected].from_file(CASES)
    if case_names:
        dataset.cases = [case for case in dataset.cases if case.name in case_names]
    for evaluator in (
        OutcomeMatches(),
        SavedAnswers(),
        Asking(),
        MessageShape(),
        NoStandaloneAcknowledgement(),
        SavedLanguage(),
        SpanishWording(),
        CacheHealth(),
        ConversationJudge(model=JUDGE, model_settings=JUDGE_SETTINGS),
    ):
        dataset.add_evaluator(evaluator)
    dataset.report_evaluators.append(ArenaNumbers())
    return dataset


def thinking_sent(model: Model, model_settings: ModelSettings) -> str:
    """What the provider receives for `thinking`: the same setting differs per model. thinking=False is MINIMAL on
    Gemini 3.5 Flash but LOW on 3.8 Flash, which has no MINIMAL."""
    settings, parameters = model.prepare_request(model_settings, ModelRequestParameters())
    # `_translate_thinking` is private in 2.61, but it's where each adapter resolves the level per model; copying
    # that logic here would drift from what is actually sent.
    translate = getattr(model, "_translate_thinking", None)
    return str(translate(settings or {}, parameters)) if translate else f"thinking={parameters.thinking}"


def arena_row(report: EvaluationReport, thinking: str, judged_by_own_family: bool) -> dict[str, str]:
    averages = report.averages()
    scores = averages.scores if averages else {}
    analyses = {analysis.title: analysis for analysis in report.analyses}
    numbers = {title: a.value for title, a in analyses.items() if isinstance(a, ScalarResult)}
    by_model = analyses.get("responses by model")
    models = ", ".join(f"{row[0]} {row[1]}" for row in by_model.rows) if isinstance(by_model, TableResult) else ""
    mark = " *" if judged_by_own_family else ""

    def show(value: float | None, digits: int = 2) -> str:
        return "–" if value is None else f"{value:.{digits}f}"

    def rate(name: str) -> float | None:
        # averages() gives one pass rate over all assertions; the ranking needs it per check.
        values = [case.assertions[name].value for case in report.cases if name in case.assertions]
        return statistics.fmean(values) if values else None

    row = {
        "config": report.name,
        "thinking sent": thinking,
        "runs": f"{len(report.cases)} ok, {len(report.failures)} failed",
        "all checks": show(averages.assertions if averages else None),
    }
    row |= {name: show(rate(name)) for name in ASSERTIONS}
    row |= {name: show(scores.get(name)) for name in SCORES}
    row |= {name: show(scores.get(name)) + mark for name in JUDGE_SCORES}
    row |= {
        "p50 s": show(numbers.get("p50 per turn"), 1),
        "p95 s": show(numbers.get("p95 per turn"), 1),
        "request p50 / p95 / p99 s": " / ".join(show(numbers.get(f"request p{p}"), 1) for p in (50, 95, 99)),
        "requests per turn (mean, max)": f"{show(numbers.get('requests per turn'))}, "
        f"{show(numbers.get('most requests in a turn'), 0)}",
        "USD / completed": show(numbers.get("cost per completed screening"), 4),
        "cache share": show(numbers.get("cache share")),
        "agent tokens: input / cache read / output": " / ".join(
            f"{numbers.get(f'agent {kind} tokens', 0):,.0f}" for kind in ("input", "cache read", "output")
        ),
        "spend: agent / simulator USD": f"{show(numbers.get('agent spend'), 3)} / "
        f"{show(numbers.get('simulator spend'), 3)}",
        "cache collapses": show(scores.get("cache_collapses")),
        "responses by model": models,
    }
    return row


def markdown_table(rows: list[dict[str, str]]) -> str:
    # One column per config and one row per check, so a wide arena still reads on GitHub and in a terminal.
    lines = ["| | " + " | ".join(row["config"] for row in rows) + " |", "|---" * (len(rows) + 1) + "|"]
    lines += [f"| {key} | " + " | ".join(row[key] for row in rows) + " |" for key in list(rows[0])[1:]]
    return "\n".join(lines)


def quote(speaker: str, message: str) -> str:
    return f"> **{speaker}** " + message.replace("\n", "\n> ") + "\n"


def write_transcripts(report: EvaluationReport, path: Path) -> None:
    """Every conversation with its tool calls and failed checks, for reading by hand."""
    parts = [f"# {report.name}\n"]
    for case in report.cases:
        failed = [f"{name}: {result.reason}" for name, result in case.assertions.items() if not result.value]
        failed += [f"{name} {score.value:.2f}: {score.reason}" for name, score in case.scores.items() if score.reason]
        parts.append(f"## {case.name}\n\nNotes: {'; '.join(failed) or 'nothing'}\n")
        run = case.output
        parts += [quote("assistant", message) for message in run.first_message]
        for turn in run.turns:
            parts += [quote("candidate", message) for message in turn.candidate]
            parts.append(
                f"`{', '.join(turn.tool_calls) or 'no tools'} · {', '.join(turn.models)} · {turn.seconds:.1f}s`"
            )
            parts += [quote("assistant", message) for message in turn.reply]
            parts.append(f"`asked: {', '.join(turn.asked_about) or '-'}`\n")
        answers = run.state.answers
        parts.append(f"Final: status={run.state.status} outcome={run.state.outcome} license={answers.license}\n")
        if run.summary:
            parts.append(f"Summary: {run.summary.model_dump_json()}\n")
    for failure in report.failures:
        parts.append(f"## {failure.name}\n\nTask failed: {failure.error_message}\n")
    path.write_text("\n".join(parts), encoding="utf-8")


async def main(args: argparse.Namespace) -> None:
    settings = Settings()
    logfire.configure(service_name=SERVICE_NAME, environment="evals", send_to_logfire="if-token-present", console=False)
    logfire.instrument_pydantic_ai()

    model_settings = ModelSettings(**(MODEL_SETTINGS | json.loads(args.settings)))
    version = prompt_version(CONFIG, model_settings)
    dataset = load_dataset(args.case)
    simulator_model = model_from_string(args.simulator, settings)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    (RESULTS / "transcripts").mkdir(parents=True, exist_ok=True)
    header = (
        f"## {stamp} · prompt {version} · {len(dataset.cases)} cases × {args.repeat}\n\n"
        f"Simulator {args.simulator}, judge {JUDGE}. Checks and scores are means over runs (1.00 = always). "
        "* = judged by a model of the same family, which may favour its style.\n\n"
    )

    async def run_experiment(model_string: str) -> dict[str, str]:
        """One config, one row. Each runs in its own task, so its agent.override doesn't leak into the others."""
        model = build_model(
            settings.model_copy(update={"screening_model": model_string, "screening_fallback_models": args.fallback})
        )
        name = " → ".join([model_string, *args.fallback]) + (f" {args.settings}" if args.settings != "{}" else "")
        task = make_task(model, simulator_model, settings.max_history_messages)
        with screening_agent.override(model=model, model_settings=model_settings):
            report = await dataset.evaluate(
                task,
                name=name,
                max_concurrency=args.concurrency,
                progress=len(args.model) == 1,
                repeat=args.repeat,
                retry_task=RETRY,
                metadata={"prompt_version": version, "simulator": args.simulator, "judge": JUDGE},
            )
        report.print(include_reasons=True, include_durations=False)
        # The settings are in the name, so two configs of one model started in the same minute get separate files.
        write_transcripts(report, RESULTS / "transcripts" / f"{stamp}-{re.sub(r'[^\w.-]+', '_', name)}.md")
        judge_family = JUDGE.partition(":")[0]
        row = arena_row(
            report,
            thinking_sent(model_from_string(model_string, settings), model_settings),
            any(m.startswith(judge_family + ":") for m in [model_string, *args.fallback]),
        )
        # Each config appends its row as soon as it finishes, so a provider that hangs can't take the others' with it.
        with (RESULTS / "arena.md").open("a", encoding="utf-8") as results:
            results.write(f"{header}{markdown_table([row])}\n\n")
        return row

    rows = await asyncio.gather(*(run_experiment(model_string) for model_string in args.model))
    print("\n" + header + markdown_table(rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", required=True, help="PydanticAI model string; repeat for an arena.")
    parser.add_argument("--fallback", action="append", default=[], help="Fallback model string, in order.")
    parser.add_argument("--settings", default="{}", help="JSON merged into the model settings.")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per case.")
    parser.add_argument("--concurrency", type=int, default=3, help="Conversations at a time (keep 2-4).")
    parser.add_argument("--case", action="append", default=[], help="Run only this case; repeatable.")
    parser.add_argument("--simulator", default=SIMULATOR, help="Model string for the simulated candidate.")
    asyncio.run(main(parser.parse_args()))
