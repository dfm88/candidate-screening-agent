from datetime import UTC, date, datetime

import pytest

from screening.config import DEFAULT_CLIENT_CONFIG, Country, Language, Locale, load_client_config
from screening.domain import (
    Application,
    Availability,
    Awaiting,
    Decision,
    FieldName,
    FieldStatus,
    LicenseAnswer,
    NotQualifiedReason,
    Outcome,
    ReviewReason,
    Schedule,
    ScreeningState,
    StartDateSpec,
    Status,
    TurnExtraction,
    decide,
    extraction_from_payload,
    is_stop_keyword,
    lookup_place,
    match_postal_code,
    normalize_platforms,
    resolve_start_date,
    transition,
)

CONFIG = load_client_config(DEFAULT_CLIENT_CONFIG)
# A Saturday morning in Madrid.
NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
MADRID = Application(full_name="Lucía Martín García", vacancy_location_id="es-mad-chamberi")
CDMX = Application(full_name="José Hernández", vacancy_location_id="mx-cdmx-roma")


def invited(language: Language = Language.ES) -> ScreeningState:
    return ScreeningState(status=Status.INVITED, language=language)


def run(state: ScreeningState, *turns: TurnExtraction, app: Application = MADRID, now: datetime = NOW) -> Decision:
    decision = Decision(state=state, locale=Locale.ES_ES)
    for ex in turns:
        decision = decide(decision.state, app, ex, now, CONFIG)
    return decision


START = TurnExtraction(wants_to_start=True)
NAME_OK = TurnExtraction(confirms_name=True)
LICENSE_YES = TurnExtraction(license=LicenseAnswer.YES)
CHAMBERI = TurnExtraction(postal_code="28010")
FULL_TIME = TurnExtraction(availability=[Availability.FULL_TIME])
EVENINGS = TurnExtraction(schedule=[Schedule.EVENING])
TWO_YEARS_GLOVO = TurnExtraction(experience_years=2, platforms=["glovo"])
ASAP = TurnExtraction(start_date=StartDateSpec(kind="asap"))
EVERYTHING_UP_TO_RECAP = (START, NAME_OK, LICENSE_YES, CHAMBERI, FULL_TIME, EVENINGS, TWO_YEARS_GLOVO, ASAP)


def asked(decision: Decision) -> FieldName | None:
    asks = [act for act in decision.acts if act.kind == "ask"]
    return asks[-1].field if asks else None


# --- Lookups and value resolution ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "country", "expected"),
    [
        ("28010", Country.ES, "es-mad-chamberi"),
        ("28905", Country.ES, "es-mad-getafe"),
        ("28001", Country.ES, None),  # Madrid, but not a served area
        ("19001", Country.ES, "es-gua-centro"),  # Guadalajara, Spain
        ("44160", Country.MX, "mx-gdl-americana"),  # Guadalajara, Mexico
        ("44160", Country.ES, None),  # the same digits are Teruel in Spain: the vacancy's country decides
    ],
)
def test_postal_code_is_read_in_the_vacancy_country(code: str, country: Country, expected: str | None) -> None:
    match = match_postal_code(code, CONFIG, country, preferred="")
    assert (match.id if match else None) == expected


def test_place_lookup_ignores_accents_case_and_colonia_prefix() -> None:
    assert [loc.id for loc in lookup_place("chamberi", CONFIG, Country.ES)] == ["es-mad-chamberi"]
    assert [loc.id for loc in lookup_place("Colonia Roma Norte", CONFIG, Country.MX)] == ["mx-cdmx-roma"]


def test_ambiguous_or_unknown_places_return_no_single_match() -> None:
    assert len(lookup_place("Centro", CONFIG, Country.MX)) == 3
    assert lookup_place("Centro", CONFIG, Country.ES) == []
    assert lookup_place("Madrid", CONFIG, Country.ES) == []


def test_platform_names_are_normalized_and_unknown_ones_kept() -> None:
    assert normalize_platforms(["uber", "GLOVO", "Pizzería Luigi", "Glovo"], CONFIG) == [
        "Uber Eats",
        "Glovo",
        "Pizzería Luigi",
    ]


SATURDAY = date(2026, 10, 24)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (StartDateSpec(kind="asap"), SATURDAY),
        (StartDateSpec(kind="weekday", weekday=0), date(2026, 10, 26)),  # "el lunes"
        (StartDateSpec(kind="weekday", weekday=5), date(2026, 10, 31)),  # "el sábado" said on a Saturday
        (StartDateSpec(kind="in", amount=2, unit="weeks"), date(2026, 11, 7)),
        (StartDateSpec(kind="date", calendar_date=date(2026, 1, 15)), date(2027, 1, 15)),  # "15 de enero"
        (StartDateSpec(kind="date", calendar_date=date(2024, 1, 15)), None),
        (StartDateSpec(kind="unclear"), None),
    ],
)
def test_start_date_resolution(spec: StartDateSpec, expected: date | None) -> None:
    assert resolve_start_date(spec, SATURDAY) == expected


def test_relative_dates_use_the_restaurant_time_zone() -> None:
    late_saturday_utc = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)  # Sunday in Madrid, Saturday in CDMX
    for app, expected in [(MADRID, date(2026, 10, 25)), (CDMX, date(2026, 10, 24))]:
        decision = run(invited(), START, ASAP, app=app, now=late_saturday_utc)
        assert decision.state.answers.start_date == expected


def test_status_transitions_reject_invalid_moves() -> None:
    assert transition(Status.INVITED, Status.IN_PROGRESS) is Status.IN_PROGRESS
    with pytest.raises(ValueError):
        transition(Status.PENDING, Status.COMPLETED)


def test_quick_reply_payloads_and_stop_keywords_skip_the_llm() -> None:
    assert extraction_from_payload("license:no") == TurnExtraction(license=LicenseAnswer.NO)
    assert extraction_from_payload("schedule:evening") == TurnExtraction(schedule=[Schedule.EVENING])
    assert extraction_from_payload("unknown") is None
    assert is_stop_keyword(" BAJA ") and is_stop_keyword("Stop") and not is_stop_keyword("stop it please")


# --- The flow ------------------------------------------------------------------------------------------


def test_first_reply_gets_the_intro_with_ai_disclosure_and_name_confirmation() -> None:
    decision = run(invited(), START)
    assert decision.state.status is Status.IN_PROGRESS
    assert "asistente virtual" in decision.template_bubbles[0]
    assert "Lucía Martín García" in decision.template_bubbles[-1]
    assert [qr.payload for qr in decision.quick_replies] == ["name:yes", "name:no"]


def test_candidate_from_the_link_gets_the_direct_intro() -> None:
    decision = run(ScreeningState(language=Language.ES), TurnExtraction())
    assert decision.template_bubbles[0].startswith("Hola 👋\nSoy Sazi")


def test_happy_path_ends_qualified_after_the_confirmed_recap() -> None:
    at_recap = run(invited(), *EVERYTHING_UP_TO_RECAP)
    assert at_recap.state.awaiting is Awaiting.RECAP
    recap = at_recap.template_bubbles[0]
    assert "28010 · Madrid Chamberí" in recap and "2 años (Glovo)" in recap and "sábado 24 de octubre" in recap

    done = run(at_recap.state, TurnExtraction(recap_confirmed=True))
    assert (done.state.status, done.state.outcome) == (Status.COMPLETED, Outcome.QUALIFIED)
    assert done.state.flags == [] and done.state.gaps == []
    assert "cumple con los requisitos" in done.template_bubbles[0]


def test_several_answers_in_one_message_are_all_kept_and_not_asked_again() -> None:
    combined = TurnExtraction(license=LicenseAnswer.YES, postal_code="28010", availability=[Availability.WEEKENDS])
    decision = run(invited(), START, NAME_OK, combined)
    assert asked(decision) is FieldName.SCHEDULE
    assert decision.state.answers.availability == [Availability.WEEKENDS]


def test_no_license_closes_not_qualified_with_reason_and_talent_pool_offer() -> None:
    decision = run(invited(), START, NAME_OK, TurnExtraction(license=LicenseAnswer.NO))
    s = decision.state
    assert (s.outcome, s.not_qualified_reason) == (Outcome.NOT_QUALIFIED, NotQualifiedReason.NO_LICENSE)
    assert "carnet de conducir en vigor" in decision.template_bubbles[0]
    assert [qr.payload for qr in decision.quick_replies] == ["talent_pool:yes", "talent_pool:no"]

    after = run(s, TurnExtraction(talent_pool_opt_in=True))
    assert after.state.talent_pool_opt_in is True and after.state.awaiting is Awaiting.NOTHING


def test_regional_wording_follows_the_country_of_work() -> None:
    decision = run(invited(), START, NAME_OK, TurnExtraction(license=LicenseAnswer.NO), app=CDMX)
    assert decision.locale is Locale.ES_MX
    assert "licencia de manejo vigente" in decision.template_bubbles[0]


def test_postal_code_outside_every_area_is_not_qualified() -> None:
    decision = run(invited(), START, NAME_OK, LICENSE_YES, TurnExtraction(postal_code="28001"))
    assert decision.state.not_qualified_reason is NotQualifiedReason.OUTSIDE_SERVICE_AREA
    assert "no repartimos en tu zona" in decision.template_bubbles[0]


def test_ambiguous_place_asks_for_the_postal_code_then_records_the_matching_restaurant() -> None:
    first = run(invited(), START, NAME_OK, LICENSE_YES, TurnExtraction(place_name="Centro"), app=CDMX)
    assert first.state.fields[FieldName.SERVICE_AREA].status is FieldStatus.UNCLEAR
    assert first.acts[-1].field is FieldName.SERVICE_AREA and first.acts[-1].attempt == 2

    second = run(first.state, TurnExtraction(postal_code="64000"), app=CDMX)
    area = second.state.answers.area
    assert area is not None and area.location_id == "mx-mty-centro"  # not the applied-to restaurant
    assert asked(second) is FieldName.AVAILABILITY


def test_foreign_license_unclear_after_two_tries_flags_review_but_screening_continues() -> None:
    foreign = TurnExtraction(license=LicenseAnswer.UNCLEAR, license_note="foreign license (VE)")
    decision = run(invited(), START, NAME_OK, foreign, foreign)
    assert decision.state.fields[FieldName.LICENSE].status is FieldStatus.UNRESOLVED
    assert decision.state.flags[0].reason is ReviewReason.UNRESOLVED_FIELD
    assert decision.state.flags[0].detail == "foreign license (VE)"
    assert asked(decision) is FieldName.SERVICE_AREA

    rest = (CHAMBERI, FULL_TIME, EVENINGS, TWO_YEARS_GLOVO, ASAP, TurnExtraction(recap_confirmed=True))
    assert run(decision.state, *rest).state.outcome is Outcome.NEEDS_HUMAN_REVIEW


def test_unclear_non_knockout_field_becomes_a_gap_without_changing_the_outcome() -> None:
    silent = TurnExtraction()
    decision = run(invited(), START, NAME_OK, LICENSE_YES, CHAMBERI, FULL_TIME, silent, silent)
    assert decision.state.fields[FieldName.SCHEDULE].status is FieldStatus.UNRESOLVED
    done = run(decision.state, TWO_YEARS_GLOVO, ASAP, TurnExtraction(recap_confirmed=True))
    assert done.state.outcome is Outcome.QUALIFIED and done.state.gaps == [FieldName.SCHEDULE]
    assert done.state.flags == []


def test_a_question_instead_of_an_answer_is_not_a_failed_try() -> None:
    decision = run(invited(), START, NAME_OK, LICENSE_YES, CHAMBERI, TurnExtraction(question="¿Cuánto pagan?"))
    assert decision.state.fields[FieldName.AVAILABILITY].tries == 0
    assert [act.kind for act in decision.acts] == ["answer", "ask"]
    assert decision.state.open_questions == ["¿Cuánto pagan?"]


def test_single_word_name_asks_once_for_the_surname_then_accepts_it() -> None:
    decision = run(invited(), START, TurnExtraction(confirms_name=False, full_name="Lucía"))
    assert asked(decision) is FieldName.FULL_NAME and decision.acts[-1].attempt == 2
    accepted = run(decision.state, TurnExtraction())
    assert accepted.state.fields[FieldName.FULL_NAME].status is FieldStatus.VALID
    assert accepted.state.flags == []


def test_knockout_answer_changed_after_closing_reopens_and_ends_in_review() -> None:
    closed = run(invited(), START, NAME_OK, TurnExtraction(license=LicenseAnswer.NO))
    reopened = run(closed.state, LICENSE_YES)
    s = reopened.state
    assert (s.status, s.outcome, s.not_qualified_reason) == (Status.IN_PROGRESS, None, None)
    assert [f.reason for f in s.flags] == [ReviewReason.KNOCKOUT_CHANGED]
    assert asked(reopened) is FieldName.SERVICE_AREA

    rest = (CHAMBERI, FULL_TIME, EVENINGS, TWO_YEARS_GLOVO, ASAP, TurnExtraction(recap_confirmed=True))
    assert run(s, *rest).state.outcome is Outcome.NEEDS_HUMAN_REVIEW


def test_asking_for_a_person_flags_review_and_offers_to_finish() -> None:
    decision = run(invited(), START, NAME_OK, TurnExtraction(wants_human=True))
    assert [f.reason for f in decision.state.flags] == [ReviewReason.HUMAN_REQUESTED]
    assert [act.kind for act in decision.acts] == ["handoff", "ask"]

    declined = run(decision.state, TurnExtraction(declines_to_continue=True))
    assert (declined.state.status, declined.state.outcome) == (Status.COMPLETED, Outcome.NEEDS_HUMAN_REVIEW)


def test_frustration_offers_a_person_once_but_is_not_a_review_reason() -> None:
    frustrated = TurnExtraction(frustrated=True)
    decision = run(invited(), START, NAME_OK, frustrated, frustrated)
    assert decision.state.simplify and decision.state.flags == []
    assert [act.kind for act in decision.acts] == ["ask"]  # offered only the first time


def test_not_qualified_candidate_asking_for_a_person_moves_to_review() -> None:
    closed = run(invited(), START, NAME_OK, TurnExtraction(license=LicenseAnswer.NO))
    decision = run(closed.state, TurnExtraction(wants_human=True))
    assert decision.state.outcome is Outcome.NEEDS_HUMAN_REVIEW
    assert "Una persona del equipo revisará" in decision.template_bubbles[-1]


def test_repeated_abuse_ends_the_conversation_for_review() -> None:
    abusive = TurnExtraction(abusive=True)
    first = run(invited(), START, NAME_OK, abusive)
    assert first.acts[0].kind == "redirect" and first.state.status is Status.IN_PROGRESS
    second = run(first.state, abusive)
    assert second.state.outcome is Outcome.NEEDS_HUMAN_REVIEW
    assert second.state.flags[-1].reason is ReviewReason.CONVERSATION_ENDED


@pytest.mark.parametrize(
    ("ex", "template_start"),
    [(TurnExtraction(not_interested=True), "Entendido"), (TurnExtraction(wrong_person=True), "Disculpa")],
)
def test_not_interested_or_wrong_person_withdraws(ex: TurnExtraction, template_start: str) -> None:
    decision = run(invited(), START, ex)
    assert decision.state.status is Status.WITHDRAWN
    assert decision.template_bubbles[0].startswith(template_start)


def test_language_follows_the_candidate_and_variant_follows_the_country() -> None:
    decision = run(invited(), START, NAME_OK, TurnExtraction(language=Language.EN, license=LicenseAnswer.NO), app=CDMX)
    assert decision.locale is Locale.EN
    assert "valid driving license" in decision.template_bubbles[0]


def test_abandoned_before_starting_still_gets_the_intro() -> None:
    abandoned = ScreeningState(status=Status.ABANDONED, language=Language.ES)
    decision = run(abandoned, TurnExtraction())
    assert "asistente virtual" in decision.template_bubbles[0]


def test_abandoned_mid_screening_resumes_where_it_stopped() -> None:
    mid = run(invited(), START, NAME_OK, LICENSE_YES).state
    mid.status = Status.ABANDONED
    decision = run(mid, TurnExtraction())
    assert [act.kind for act in decision.acts] == ["resume", "ask"]
    assert asked(decision) is FieldName.SERVICE_AREA


def test_recap_correction_shows_the_recap_again() -> None:
    at_recap = run(invited(), *EVERYTHING_UP_TO_RECAP).state
    corrected = run(at_recap, TurnExtraction(postal_code="28905"))
    assert corrected.state.awaiting is Awaiting.RECAP
    assert "Getafe" in corrected.template_bubbles[0]

    unsure = run(at_recap, TurnExtraction(recap_confirmed=False))
    assert [act.kind for act in unsure.acts] == ["ask_correction"]
