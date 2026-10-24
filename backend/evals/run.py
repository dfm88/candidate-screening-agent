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
import statistics
from datetime import UTC, datetime
from pathlib import Path

import logfire
from pydantic_ai.exceptions import ModelHTTPError, ModelRateLimitError, ModelUnavailableError
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
    ReplyLanguage,
    SavedAnswers,
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
SCORES = ["saved_answers", "message_shape", "reply_language"]


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
        ReplyLanguage(),
        SpanishWording(),
        CacheHealth(),
        ConversationJudge(model=JUDGE, model_settings=JUDGE_SETTINGS),
    ):
        dataset.add_evaluator(evaluator)
    dataset.report_evaluators.append(ArenaNumbers())
    return dataset


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def arena_row(report: EvaluationReport, judged_by_own_family: bool) -> dict[str, str]:
    cases = report.cases

    def rate(name: str) -> float | None:
        return mean([float(case.assertions[name].value) for case in cases if name in case.assertions])

    def score(name: str) -> float | None:
        return mean([float(case.scores[name].value) for case in cases if name in case.scores])

    analyses = {analysis.title: analysis for analysis in report.analyses}
    numbers = {title: a.value for title, a in analyses.items() if isinstance(a, ScalarResult)}
    by_model = analyses.get("responses by model")
    models = ", ".join(f"{row[0]} {row[1]}" for row in by_model.rows) if isinstance(by_model, TableResult) else ""
    mark = " *" if judged_by_own_family else ""

    def show(value: float | None, digits: int = 2) -> str:
        return "–" if value is None else f"{value:.{digits}f}"

    row = {"config": report.name, "runs": f"{len(cases)} ok, {len(report.failures)} failed"}
    row["all checks"] = show(
        mean([float(c.assertions[n].value) for c in cases for n in ASSERTIONS if n in c.assertions])
    )
    row |= {name: show(rate(name)) for name in ASSERTIONS}
    row |= {name: show(score(name)) for name in SCORES}
    row |= {name: show(score(name)) + mark for name in RUBRICS}
    row |= {
        "p50 s": show(numbers.get("p50 per turn"), 1),
        "p95 s": show(numbers.get("p95 per turn"), 1),
        "USD / completed": show(numbers.get("cost per completed screening"), 4),
        "cache share": show(numbers.get("cache share")),
        "cache collapses": show(score("cache_collapses")),
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
        parts.append(f"Final: status={run.state.status} outcome={run.state.outcome}\n")
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
        write_transcripts(report, RESULTS / "transcripts" / f"{stamp}-{model_string.replace(':', '_')}.md")
        judge_family = JUDGE.partition(":")[0]
        return arena_row(report, any(m.startswith(judge_family + ":") for m in [model_string, *args.fallback]))

    rows = await asyncio.gather(*(run_experiment(model_string) for model_string in args.model))
    table = markdown_table(rows)
    summary = (
        f"## {stamp} · prompt {version} · {len(dataset.cases)} cases × {args.repeat}\n\n"
        f"Simulator {args.simulator}, judge {JUDGE}. Checks and scores are means over runs (1.00 = always). "
        "* = judged by a model of the same family, which may favour its style.\n\n"
        f"{table}\n"
    )
    print("\n" + summary)
    with (RESULTS / "arena.md").open("a", encoding="utf-8") as results:
        results.write(summary + "\n")


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
