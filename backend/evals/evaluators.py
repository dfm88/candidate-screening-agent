"""Evaluators: the process design's rules as deterministic checks, an LLM judge for tone and language, and the
per-experiment numbers for the arena (latency, cost, cache share, which model answered)."""

import asyncio
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass

from pydantic_ai.settings import ModelSettings
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext, ReportEvaluator
from pydantic_evals.evaluators.llm_as_a_judge import judge_input_output
from pydantic_evals.evaluators.report_evaluator import ReportEvaluatorContext
from pydantic_evals.reporting.analyses import ReportAnalysis, ScalarResult, TableResult

from evals.task import CONFIG, Expected, Persona, ScreeningRun
from screening.config import Country, Language
from screening.domain import FieldName, ScreeningState, Status

Context = EvaluatorContext[Persona, ScreeningRun, Expected]


def normalize(text: str) -> str:
    without_accents = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", without_accents.casefold()).strip()


def word_count(text: str) -> int:
    return len(text.split())


# --- Outcome and saved answers ---------------------------------------------------------------------------------


@dataclass
class OutcomeMatches(Evaluator[Persona, ScreeningRun, Expected]):
    """Status, outcome, review reasons, unclear fields and the talent-pool answer: what the recruiter acts on."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        expected, state = ctx.metadata, ctx.output.state
        actual = {
            "status": state.status,
            "outcome": state.outcome,
            "review_reasons": sorted({flag.reason for flag in state.flags}),
            "unclear": sorted(state.unclear),
            "talent_pool_opt_in": state.talent_pool_opt_in,
        }
        wanted = {
            "status": expected.status,
            "outcome": expected.outcome,
            "review_reasons": sorted(set(expected.review_reasons)),
            "unclear": sorted(expected.unclear),
            "talent_pool_opt_in": expected.talent_pool_opt_in,
        }
        wrong = [f"{key}: {actual[key]} (expected {wanted[key]})" for key in wanted if actual[key] != wanted[key]]
        return {"outcome": EvaluationReason(value=not wrong, reason="; ".join(wrong) or None)}


@dataclass
class SavedAnswers(Evaluator[Persona, ScreeningRun, Expected]):
    """Share of the expected answers saved as the candidate gave them."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        expected, answers = ctx.metadata.answers, ctx.output.state.answers
        experience = answers.experience
        actual = {
            "full_name": answers.full_name and normalize(answers.full_name),
            "license": answers.license,
            "location_id": answers.area.location_id if answers.area else "not saved",
            "availability": set(answers.availability) or None,
            "schedule": set(answers.schedule) or None,
            "experience_years": experience.years if experience else None,
            "platforms": {normalize(p) for p in experience.platforms or []} if experience else None,
            "start_date": answers.start_date,
        }
        wanted = {
            "full_name": expected.full_name and normalize(expected.full_name),
            "license": expected.license,
            "location_id": expected.location_id,
            "availability": set(expected.availability or []) or None,
            "schedule": set(expected.schedule or []) or None,
            "experience_years": expected.experience_years,
            "platforms": {normalize(p) for p in expected.platforms or []},
            "start_date": expected.start_date,
        }
        checked = expected.model_fields_set
        wrong = [
            f"{field}: {actual[field]} (expected {wanted[field]})"
            for field in checked
            if actual[field] != wanted[field]
        ]
        score = 1 - len(wrong) / len(checked) if checked else 1.0
        return {"saved_answers": EvaluationReason(value=score, reason="; ".join(wrong) or None)}


# --- How the agent asks ------------------------------------------------------------------------------------------

KNOCKOUTS = (FieldName.LICENSE, FieldName.SERVICE_AREA)
AFTER_KNOCKOUTS = {FieldName.AVAILABILITY, FieldName.SCHEDULE, FieldName.EXPERIENCE, FieldName.START_DATE}


def answered(state: ScreeningState, field: FieldName) -> bool:
    return state.is_saved(field) or field in state.unclear


@dataclass
class Asking(Evaluator[Persona, ScreeningRun, Expected]):
    """Knockouts first (and nothing after a knockout "no"), nothing asked again once answered, at most two tries."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        out_of_order: list[str] = []
        reasked: list[str] = []
        tries: Counter[FieldName] = Counter()
        for number, turn in enumerate(ctx.output.turns, start=1):
            fields = [asked for asked in turn.asked_about if isinstance(asked, FieldName)]
            later = AFTER_KNOCKOUTS.intersection(fields)
            if later and turn.state.knockout:
                out_of_order.append(f"turn {number}: asked {sorted(later)} after a knockout")
            elif later and not all(answered(turn.state, field) for field in KNOCKOUTS):
                out_of_order.append(f"turn {number}: asked {sorted(later)} before the knockouts")
            for field in fields:
                if answered(turn.state, field):
                    reasked.append(f"turn {number}: {field}")
                else:
                    tries[field] += 1
        over = [f"{field} asked {count} times" for field, count in tries.items() if count > 2]
        return {
            "knockouts_first": EvaluationReason(value=not out_of_order, reason="; ".join(out_of_order) or None),
            "no_reask": EvaluationReason(value=not reasked, reason="; ".join(reasked) or None),
            "two_tries_max": EvaluationReason(value=not over, reason="; ".join(over) or None),
        }


# --- How the agent writes ----------------------------------------------------------------------------------------


@dataclass
class MessageShape(Evaluator[Persona, ScreeningRun, Expected]):
    """Share of turns that read like messaging: at most 3 messages of at most 40 words, at most one question, and
    the question in the last message (a short reason after it in that message is fine). The recap is exempt from the
    length and question limits."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        problems: list[str] = []
        for number, turn in enumerate(ctx.output.turns, start=1):
            recap = "recap" in turn.asked_about
            questions = sum(message.count("?") for message in turn.reply)
            if len(turn.reply) > 3:
                problems.append(f"turn {number}: {len(turn.reply)} messages")
            if not recap and any(word_count(message) > 40 for message in turn.reply):
                problems.append(f"turn {number}: a message over 40 words")
            if not recap and questions > 1:
                problems.append(f"turn {number}: {questions} questions")
            if questions and "?" not in turn.reply[-1]:
                problems.append(f"turn {number}: the question isn't in the last message")
        bad_turns = {problem.split(":")[0] for problem in problems}
        score = 1 - len(bad_turns) / len(ctx.output.turns) if ctx.output.turns else 1.0
        return {"message_shape": EvaluationReason(value=score, reason="; ".join(problems) or None)}


@dataclass
class NoStandaloneAcknowledgement(Evaluator[Persona, ScreeningRun, Expected]):
    """An acknowledgement or greeting ("Genial.", "¡Hola, Marta!") is never a message of its own before the next."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        found = [
            message
            for turn in ctx.output.turns
            for message, _next in zip(turn.reply, turn.reply[1:], strict=False)
            if word_count(message) <= 3
        ]
        reason = ", ".join(repr(message) for message in found) or None
        return {"no_standalone_ack": EvaluationReason(value=not found, reason=reason)}


# Word lists read better as text than as hundreds of quoted literals.
SPANISH_WORDS = set(
    "de la que el en y los se del las un por con no una su para es al lo como más pero ya este sí porque esta "  # noqa: SIM905
    "muy sin también me hay donde desde todo nos tu te tú mi tengo puedo vivo trabajo gracias hola qué cuándo "
    "dónde estás eres tienes perfecto bueno pues vale".split()
)
ENGLISH_WORDS = set(
    "the and to of a in is it you that i for on with have my be are can do not this but what your me we so just "  # noqa: SIM905
    "live work yes thanks hi hello great when where how there please would like".split()
)


def detect_language(text: str) -> Language | None:
    words = re.findall(r"[a-záéíóúñü]+", text.casefold())
    spanish = sum(word in SPANISH_WORDS for word in words) + text.count("¿") + text.count("¡")
    english = sum(word in ENGLISH_WORDS for word in words)
    if spanish == english:
        return None
    return Language.ES if spanish > english else Language.EN


@dataclass
class ReplyLanguage(Evaluator[Persona, ScreeningRun, Expected]):
    """Each reply is in the language of the candidate's last full sentence (a lone "ok" doesn't count); the saved
    language matches at the end."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        expected = ctx.inputs.application_language
        wrong: list[str] = []
        checked = 0
        for number, turn in enumerate(ctx.output.turns, start=1):
            sentences = re.split(r"(?<=[.!?])\s+", " ".join(turn.candidate))
            full = [sentence for sentence in sentences if word_count(sentence) >= 3]
            if full and (language := detect_language(full[-1])):
                expected = language
            actual = detect_language(" ".join(turn.reply))
            if actual is not None:
                checked += 1
                if actual != expected:
                    wrong.append(f"turn {number}: {actual} (expected {expected})")
        saved = ctx.output.state.language
        return {
            "reply_language": EvaluationReason(
                value=1 - len(wrong) / checked if checked else 1.0, reason="; ".join(wrong) or None
            ),
            "saved_language": EvaluationReason(
                value=saved == ctx.metadata.final_language,
                reason=None if saved == ctx.metadata.final_language else f"{saved}",
            ),
        }


GENDERED_WORDS = set(
    "bienvenido bienvenida interesado interesada seleccionado seleccionada contratado contratada preparado "  # noqa: SIM905
    "preparada dispuesto dispuesta inscrito inscrita".split()
)


@dataclass
class SpanishWording(Evaluator[Persona, ScreeningRun, Expected]):
    """The country of work's regional terms only (glossary in the client config), and no gendered words about the
    candidate."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        country = CONFIG.locations_by_id[ctx.inputs.vacancy].country
        other_terms = {row[other] for row in CONFIG.regional_terms for other in Country if row[other] != row[country]}
        text = normalize(" ".join(message for turn in ctx.output.turns for message in turn.reply))
        regional = [term for term in other_terms if re.search(rf"\b{re.escape(normalize(term))}\b", text)]
        gendered = [word for word in GENDERED_WORDS if re.search(rf"\b{word}\b", text)]
        return {
            "regional_terms": EvaluationReason(value=not regional, reason=", ".join(regional) or None),
            "gender_neutral": EvaluationReason(value=not gendered, reason=", ".join(gendered) or None),
        }


@dataclass
class CacheHealth(Evaluator[Persona, ScreeningRun, Expected]):
    """Prompt-cache collapses flagged by PydanticAI's instrumentation (pydantic_ai.cache.collapsed on model-request
    spans): a request that read back far less of the cached prefix than the conversation had established. With an
    append-only history it should stay at zero."""

    def evaluate(self, ctx: Context) -> dict[str, int]:
        collapsed = ctx.span_tree.find(lambda node: node.attributes.get("pydantic_ai.cache.collapsed") is True)
        return {"cache_collapses": len(collapsed)}


# --- The judge ---------------------------------------------------------------------------------------------------

RUBRICS = {
    "tone": "The assistant sounds like a friendly colleague from a hiring team: warm, brief and honest; never pushy, "
    "robotic, condescending or over-enthusiastic; it never assumes the candidate's gender. Score 1.0 when every "
    "assistant message fits, lower for each lapse.",
    "naturalness": "The assistant's messages read like chat messages a person sends from a phone: short and varied, "
    "no stock phrase repeated turn after turn, nothing form-like or email-like, one thing at a time. Score 1.0 when "
    "natural throughout, lower for each stilted or repetitive message.",
    "language_quality": "The assistant writes fluent, correct text in the language the candidate writes in, with no "
    "grammar or spelling mistakes. In Spanish it uses the variant of the country of work given in the input (Spain "
    "or Mexico), with tú, and no words typical of the other country. Score 1.0 when flawless, lower for each error.",
}


@dataclass
class ConversationJudge(Evaluator[Persona, ScreeningRun, Expected]):
    """LLM judge for what code can't check. It sees the transcript and the country only, not the state dumps that
    LLMJudge would serialize from the whole output, so it grades the conversation the candidate saw."""

    model: str
    model_settings: ModelSettings | None = None

    async def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        country = CONFIG.locations_by_id[ctx.inputs.vacancy].country
        context = f"Country of work: {'Spain' if country is Country.ES else 'Mexico'}."
        transcript = ctx.output.transcript()
        gradings = await asyncio.gather(
            *(
                judge_input_output(context, transcript, rubric, self.model, self.model_settings)
                for rubric in RUBRICS.values()
            )
        )
        return {
            name: EvaluationReason(value=grading.score, reason=grading.reason)
            for name, grading in zip(RUBRICS, gradings, strict=True)
        }


# --- Experiment numbers for the arena ------------------------------------------------------------------------------


@dataclass
class ArenaNumbers(ReportEvaluator[Persona, ScreeningRun, Expected]):
    """p50/p95 latency per turn, cost per completed screening, share of input tokens read from the cache, and which
    model answered (it matters with a fallback)."""

    def evaluate(self, ctx: ReportEvaluatorContext[Persona, ScreeningRun, Expected]) -> list[ReportAnalysis]:
        runs = [case.output for case in ctx.report.cases]
        turns = [turn for run in runs for turn in run.turns]
        if len(turns) < 2:
            return []
        seconds = sorted(turn.seconds for turn in turns)
        completed = sum(run.state.status is Status.COMPLETED for run in runs)
        costs = [turn.cost_usd for turn in turns]
        input_tokens = sum(turn.input_tokens for turn in turns)
        models = Counter(model for turn in turns for model in turn.models)
        analyses: list[ReportAnalysis] = [
            ScalarResult(title="p50 per turn", value=round(statistics.median(seconds), 2), unit="s"),
            ScalarResult(title="p95 per turn", value=round(statistics.quantiles(seconds, n=20)[-1], 2), unit="s"),
            ScalarResult(title="cache share", value=round(sum(t.cache_read_tokens for t in turns) / input_tokens, 3)),
            TableResult(
                title="responses by model",
                columns=["model", "responses"],
                rows=[[model, count] for model, count in models.most_common()],
            ),
        ]
        if completed and None not in costs:
            cost = sum(cost or 0 for cost in costs) / completed
            analyses.append(ScalarResult(title="cost per completed screening", value=round(cost, 5), unit="USD"))
        return analyses
