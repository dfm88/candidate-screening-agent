"""Evaluators: the process design's rules as deterministic checks, an LLM judge for tone, language and the summary,
and the per-experiment numbers for the arena (latency, cost, cache share, which model answered)."""

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
from screening import domain
from screening.config import Country
from screening.domain import FieldName, ScreeningState, Status

Context = EvaluatorContext[Persona, ScreeningRun, Expected]


def expected_of(ctx: Context) -> Expected:
    # Pydantic Evals types metadata as optional; every case in cases.yaml has its expectations there.
    assert ctx.metadata is not None, f"case {ctx.name} has no expectations in its metadata"
    return ctx.metadata


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
        expected, state = expected_of(ctx), ctx.output.state
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
        expected, answers = expected_of(ctx).answers, ctx.output.state.answers
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
            # With several accepted, the one saved is right when it's among them.
            "start_date": answers.start_date
            if isinstance(expected.start_date, list) and answers.start_date in expected.start_date
            else expected.start_date,
        }
        checked = expected.model_fields_set
        wrong = [
            f"{field}: {actual[field]} (expected {wanted[field]})"
            for field in checked
            if actual[field] != wanted[field]
        ]
        score = 1 - len(wrong) / len(checked) if checked else 1.0
        return {"saved_answers": EvaluationReason(value=score, reason="; ".join(wrong) or None)}


@dataclass
class GroundedAnswers(Evaluator[Persona, ScreeningRun, Expected]):
    """Nothing saved that the candidate didn't give: every field a turn saved was provided by then (the simulator's
    labels, or the script's). Whether the value saved is the one they gave is the judge's call (saved_values)."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        provided: set[FieldName] = set()
        invented: list[str] = []
        for number, (turn, saved) in enumerate(zip(ctx.output.turns, ctx.output.saved_by_turn(), strict=True), 1):
            provided.update(turn.provided)
            invented += [f"turn {number}: {field}={value}" for field, value in saved.items() if field not in provided]
        return {"grounded": EvaluationReason(value=not invented, reason="; ".join(invented) or None)}


@dataclass
class TopicsLoaded(Evaluator[Persona, ScreeningRun, Expected]):
    """The FAQ topics a case's questions need were loaded: the agent has no other source for the job's facts. Whether
    the answer stays within them is the judge's call (faq_answers)."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        wanted = expected_of(ctx).topics
        if not wanted:
            return {}
        loaded = {topic for turn in ctx.output.turns for topic in turn.topics_loaded}
        missing = sorted(set(wanted) - loaded)
        reason = f"not loaded: {', '.join(missing)}" if missing else None
        return {"faq_topics": EvaluationReason(value=not missing, reason=reason)}


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


@dataclass
class SavedLanguage(Evaluator[Persona, ScreeningRun, Expected]):
    """The language saved at the end, which reminders and the close use. Whether each reply follows the candidate's
    language is the judge's call (language_quality): word-list heuristics gave false positives."""

    def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        saved, wanted = ctx.output.state.language, expected_of(ctx).final_language
        return {"saved_language": EvaluationReason(value=saved == wanted, reason=None if saved == wanted else saved)}


GENDERED_WORDS = set(
    "bienvenido bienvenida interesado interesada seleccionado seleccionada contratado contratada preparado "  # noqa: SIM905
    "preparada dispuesto dispuesta inscrito inscrita repartidor repartidora".split()
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
    "no stock phrase repeated turn after turn, nothing form-like or email-like, one thing at a time. The recap before "
    "the close is the one place for a short list of the answers, as the process requires: don't count it as "
    "form-like. Score 1.0 when natural throughout, lower for each stilted or repetitive message.",
    "language_quality": "Each assistant reply is in the language of the candidate's last full sentence, Spanish or "
    "English (a lone word like 'ok' doesn't switch it), and is fluent and correct, with no grammar or spelling "
    "mistakes. In Spanish it uses the variant of the country of work given in the input (Spain or Mexico), with tú, "
    "and no words typical of the other country. Score 1.0 when flawless, lower for each reply in the wrong language "
    "or each error.",
}
# Judged only in conversations where the hiring team's decision reaches the candidate.
DECISION_RUBRIC = (
    "The line marked 'Hiring team (decision)' is the recruiters' decision on the application, and it is final. Every "
    "assistant reply after it must agree with it. A reply contradicts it when it says or implies that the application "
    "is still to be reviewed or decided, or when it states or implies a different outcome, for example that a "
    "requirement isn't met after a new answer. Taking a new answer and saying the team will see it is fine. Score 1.0 "
    "when every later reply agrees with the decision, 0.0 when any contradicts it."
)
# Judged in every conversation that saved something, scripted ones too: a fragment's saves are as real as a whole one's.
SAVED_VALUES_RUBRIC = (
    "Under each candidate turn, a 'Saved' line lists the answers the assistant saved in that turn, as the system "
    "stored them, and the arguments it passed to save them. Each saved value must be what the candidate said up to "
    "then, read in the input's date and country: nothing the candidate didn't say, nothing narrowed or widened (an "
    "'any time' schedule is flexible, not every option; 'weekends' alone is not part-time), postal codes and names as "
    "they wrote them, a relative date counted from today (a bare weekday is the next one, never today). An unclear "
    "answer saved as yes or no is wrong. The restaurant named with an area is the system's lookup of the place or "
    "postal code, not something the candidate has to say; where a field has fixed options, a nuance they can't hold "
    "(how often, a preference) is no mismatch. Score 1.0 when every saved value holds, 0.0 when any was invented or "
    "doesn't match what the candidate said."
)
# Judged in conversations tagged faq and in any that loaded a topic, scripted ones too.
FAQ_RUBRIC = (
    "The input lists the FAQ topics the assistant loaded: the only facts about the job it may share. Every answer to "
    "a candidate's question about the job must come from them, in the assistant's own words, for the country of work: "
    "nothing added, nothing changed. What the assistant says about this chat itself (the first message, who sees the "
    "answers, the next question) is no answer. A question the topics don't answer gets a reply that the team will "
    "check, never a guess. Asked "
    "whether a place is covered, the assistant says neither yes nor no and asks for the postal code. Score 1.0 when "
    "every answer holds, 0.0 when any adds or changes a fact, guesses, or says whether a place is covered."
)
SUMMARY_RUBRIC = (
    "The output is the part of a recruiter's summary that adds to the key data in the input, which the recruiter "
    "already sees: caveats to check before deciding, and notes worth knowing. Empty lists are right when the "
    "transcript holds nothing the key data lack; what the key data already say, in any wording, is no omission. "
    "Each item must come from something the candidate said in the transcript, must not contradict it or restate the "
    "key data, and must make no guess about the person (reliability, motivation, age, origin, gender). Score 1.0 when "
    "every item holds and nothing the recruiter should know beyond the key data is left out; lower for each item that "
    "breaks a rule and for each such omission."
)


@dataclass
class ConversationJudge(Evaluator[Persona, ScreeningRun, Expected]):
    """LLM judge for what code can't check. It sees the transcript, the country and the name, not the state dumps that
    LLMJudge would serialize from the whole output, so it grades the conversation the candidate saw. The summary is
    graded against the transcript. A scripted case is a fragment, not a conversation: only its decision and its saved
    values are judged."""

    model: str
    model_settings: ModelSettings | None = None

    async def evaluate(self, ctx: Context) -> dict[str, EvaluationReason]:
        country = CONFIG.locations_by_id[ctx.inputs.vacancy].country
        # The name comes from the application, so the assistant may use it before the candidate gives it.
        context = (
            f"Country of work: {'Spain' if country is Country.ES else 'Mexico'}. "
            f"Name on the application: {ctx.inputs.name_on_application}."
        )
        transcript = ctx.output.transcript()
        rubrics = {} if ctx.inputs.script else dict(RUBRICS)
        if ctx.inputs.decision:
            rubrics["decision"] = DECISION_RUBRIC
        gradings = {
            name: judge_input_output(context, transcript, rubric, self.model, self.model_settings)
            for name, rubric in rubrics.items()
        }
        if any(ctx.output.saved_by_turn()):
            location = CONFIG.locations_by_id[ctx.inputs.vacancy]
            gradings["saved_values"] = judge_input_output(
                f"{context} Today: {ctx.inputs.now.astimezone(location.tz):%Y-%m-%d (%A)}.",
                ctx.output.transcript(with_saves=True),
                SAVED_VALUES_RUBRIC,
                self.model,
                self.model_settings,
            )
        topics = {topic: text for turn in ctx.output.turns for topic, text in turn.topics_loaded.items()}
        if topics or "faq" in expected_of(ctx).tags:
            loaded = "\n\n".join(f"<topic id={topic!r}>\n{text}\n</topic>" for topic, text in topics.items())
            gradings["faq_answers"] = judge_input_output(
                f"{context}\nTopics the assistant loaded:\n{loaded or 'none'}",
                transcript,
                FAQ_RUBRIC,
                self.model,
                self.model_settings,
            )
        if ctx.output.summary and not ctx.inputs.script:
            # The summary adds to the key data (what the recruiter sees first), so the judge reads both. Today's date
            # too, as the summary agent does: a start date of today next to "right away" is no discrepancy.
            key_data = domain.key_data(ctx.output.state, CONFIG).model_dump_json(indent=2)
            today = f"{ctx.inputs.now.astimezone(CONFIG.locations_by_id[ctx.inputs.vacancy].tz):%Y-%m-%d (%A)}"
            gradings["summary"] = judge_input_output(
                f"<today>{today}</today>\n<key_data>\n{key_data}\n</key_data>\n<transcript>\n{transcript}\n</transcript>",
                ctx.output.summary.model_dump_json(indent=2),
                SUMMARY_RUBRIC,
                self.model,
                self.model_settings,
            )
        results = await asyncio.gather(*gradings.values())
        return {
            name: EvaluationReason(value=grading.score, reason=grading.reason)
            for name, grading in zip(gradings, results, strict=True)
        }


# --- Experiment numbers for the arena ------------------------------------------------------------------------------


@dataclass
class ArenaNumbers(ReportEvaluator[Persona, ScreeningRun, Expected]):
    """p50/p95 latency per turn, latency per model request and requests per turn (they set the timeouts in
    settings.py), cost per completed screening, share of input tokens read from the cache, and which model answered (it
    matters with a fallback)."""

    def evaluate(self, ctx: ReportEvaluatorContext[Persona, ScreeningRun, Expected]) -> list[ReportAnalysis]:
        runs = [case.output for case in ctx.report.cases]
        turns = [turn for run in runs for turn in run.turns]
        if len(turns) < 2:
            return []
        seconds = sorted(turn.seconds for turn in turns)
        request_seconds = [value for turn in turns for value in turn.request_seconds]
        request_percentiles = statistics.quantiles(request_seconds, n=100) if len(request_seconds) > 1 else []
        requests = [len(turn.models) for turn in turns]
        completed = sum(run.state.status is Status.COMPLETED for run in runs)
        costs = [turn.cost_usd for turn in turns]
        input_tokens = sum(turn.input_tokens for turn in turns)
        models = Counter(model for turn in turns for model in turn.models)
        topic_loads = Counter(topic for turn in turns for topic in turn.topics_loaded)
        # A load of a topic already loaded is bounced with a retry, which costs a request.
        bounced = sum(turn.tool_calls.count("load_capability") - len(turn.topics_loaded) for turn in turns)
        analyses: list[ReportAnalysis] = [
            ScalarResult(title="p50 per turn", value=round(statistics.median(seconds), 2), unit="s"),
            ScalarResult(title="p95 per turn", value=round(statistics.quantiles(seconds, n=20)[-1], 2), unit="s"),
            ScalarResult(title="cache share", value=round(sum(t.cache_read_tokens for t in turns) / input_tokens, 3)),
            ScalarResult(title="requests per turn", value=round(statistics.fmean(requests), 2)),
            ScalarResult(title="most requests in a turn", value=max(requests)),
            TableResult(
                title="responses by model",
                columns=["model", "responses"],
                rows=[[model, count] for model, count in models.most_common()],
            ),
            TableResult(
                title="topic loads",
                columns=["topic", "loads"],
                rows=[[topic, count] for topic, count in topic_loads.most_common()],
            ),
            ScalarResult(title="bounced topic loads", value=bounced),
        ]
        if request_percentiles:
            analyses += [
                ScalarResult(title=f"request p{p}", value=round(request_percentiles[p - 1], 2), unit="s")
                for p in (50, 95, 99)
            ]
        # What the row cost to run: the agent's turns and the simulator. The summary and the judge are a few cents.
        # The agent's tokens too, to reprice a row when genai-prices lags the providers' price lists.
        analyses += [
            ScalarResult(title="agent input tokens", value=input_tokens),
            ScalarResult(title="agent cache read tokens", value=sum(turn.cache_read_tokens for turn in turns)),
            ScalarResult(title="agent output tokens", value=sum(turn.output_tokens for turn in turns)),
            ScalarResult(title="agent spend", value=round(sum(cost or 0 for cost in costs), 4), unit="USD"),
            ScalarResult(
                title="simulator spend",
                value=round(sum(case.metrics.get("simulator_cost_usd", 0) for case in ctx.report.cases), 4),
                unit="USD",
            ),
        ]
        if completed and None not in costs:
            cost = sum(cost or 0 for cost in costs) / completed
            analyses.append(ScalarResult(title="cost per completed screening", value=round(cost, 5), unit="USD"))
        return analyses
