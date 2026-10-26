from datetime import UTC, date, datetime

import pytest

from screening.config import DEFAULT_CLIENT_CONFIG, Country, Language, load_client_config
from screening.domain import (
    Application,
    Availability,
    EndReason,
    FieldName,
    LicenseAnswer,
    NewAnswers,
    NotQualifiedReason,
    Outcome,
    ReviewFlag,
    ReviewReason,
    Schedule,
    ScreeningState,
    StartDateSpec,
    Status,
    end_conversation,
    finish_screening,
    key_data,
    lookup_place,
    match_postal_code,
    normalize_platforms,
    request_human,
    resolve_start_date,
    save_answers,
    transition,
)

CONFIG = load_client_config(DEFAULT_CLIENT_CONFIG)
NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)  # a Saturday morning in Madrid
MADRID = Application(full_name="Lucía Martín García", vacancy_location_id="es-mad-chamberi", language=Language.ES)
CDMX = Application(full_name="José Hernández", vacancy_location_id="mx-cdmx-roma", language=Language.ES)
BARCELONA = Application(full_name="Marc Vidal Roca", vacancy_location_id="es-bcn-gracia", language=Language.ES)
EVERYTHING = NewAnswers(
    full_name="Lucía Martín García",
    license=LicenseAnswer.YES,
    postal_code="28010",
    availability=[Availability.FULL_TIME],
    schedule=[Schedule.EVENING],
    experience_years=2,
    platforms=["glovo"],
    start_date=StartDateSpec(kind="asap"),
)


def in_progress() -> ScreeningState:
    return ScreeningState(status=Status.IN_PROGRESS, language=Language.ES)


def save(
    new: NewAnswers,
    state: ScreeningState | None = None,
    app: Application = MADRID,
    now: datetime = NOW,
    decision: Outcome | None = None,
):
    return save_answers(state or in_progress(), new, app, CONFIG, now, decision)


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
    assert len(lookup_place("Centro", CONFIG, Country.MX)) == 3
    assert lookup_place("Madrid", CONFIG, Country.ES) == []


@pytest.mark.parametrize("name", ["Barcelona, Gràcia", "en Gràcia, Barcelona", "Gracia,barcelona", "in Gràcia"])
def test_place_lookup_reads_a_place_given_with_its_city(name: str) -> None:
    assert [loc.id for loc in lookup_place(name, CONFIG, Country.ES)] == ["es-bcn-gracia"]


def test_the_city_given_with_a_place_picks_one_of_its_areas() -> None:
    assert [loc.id for loc in lookup_place("Centro, Monterrey", CONFIG, Country.MX)] == ["mx-mty-centro"]
    assert len(lookup_place("Centro, Cancún", CONFIG, Country.MX)) == 3  # a city of none of them picks none


def test_platform_names_are_normalized_and_unknown_ones_kept() -> None:
    names = ["uber", "GLOVO", "Pizzería Luigi", "Glovo"]
    assert normalize_platforms(names, CONFIG) == ["Uber Eats", "Glovo", "Pizzería Luigi"]


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
        (StartDateSpec(kind="in"), None),
    ],
)
def test_start_date_resolution(spec: StartDateSpec, expected: date | None) -> None:
    assert resolve_start_date(spec, SATURDAY) == expected


def test_status_transitions_reject_invalid_moves() -> None:
    assert transition(Status.INVITED, Status.IN_PROGRESS) is Status.IN_PROGRESS
    with pytest.raises(ValueError):
        transition(Status.PENDING, Status.COMPLETED)


# --- save_answers ---------------------------------------------------------------------------------------------


def test_several_answers_at_once_are_saved_and_the_rest_listed_in_order() -> None:
    new = NewAnswers(license=LicenseAnswer.YES, postal_code="28010", availability=[Availability.WEEKENDS])
    state, result = save(new)
    assert result.saved == {
        FieldName.LICENSE: "yes",
        FieldName.SERVICE_AREA: "lives at 28010, in the area of the Madrid Chamberí restaurant, in Madrid",
        FieldName.AVAILABILITY: "weekends",
    }
    assert result.still_to_ask == [FieldName.FULL_NAME, FieldName.SCHEDULE, FieldName.EXPERIENCE, FieldName.START_DATE]
    assert result.knockout is None and state.answers.area and state.answers.area.location_id == "es-mad-chamberi"


def test_postal_code_outside_every_area_is_a_knockout() -> None:
    state, result = save(NewAnswers(postal_code="28001"))
    assert state.knockout is NotQualifiedReason.OUTSIDE_SERVICE_AREA
    assert result.saved[FieldName.SERVICE_AREA] == "lives at 28001, outside every service area"
    assert result.knockout == "outside_service_area: the postal code saved, 28001, is outside every service area"


def test_an_outside_code_says_which_area_from_a_named_place_it_replaced() -> None:
    # The candidate named Gràcia, then gave a code outside every area: the agent checks which is right first.
    state, _ = save(NewAnswers(place="Gràcia"), app=BARCELONA)
    state, result = save(NewAnswers(postal_code="08011"), state=state, app=BARCELONA)
    assert state.knockout is NotQualifiedReason.OUTSIDE_SERVICE_AREA
    assert result.knockout == (
        "outside_service_area: the postal code saved, 08011, is outside every service area. It replaced Gràcia, which "
        "they named before, in the Barcelona Gràcia area: the two disagree"
    )
    # A code replacing a code is a correction they gave: nothing to say about the one before.
    state, result = save(NewAnswers(postal_code="08001"), state=state, app=BARCELONA)
    assert result.knockout and "replaced" not in result.knockout


def test_a_code_in_another_of_our_areas_is_saved_and_named_with_its_city() -> None:
    state, result = save(NewAnswers(postal_code="46005"), app=BARCELONA)
    assert state.knockout is None and state.answers.area and state.answers.area.location_id == "es-vlc-ruzafa"
    assert result.saved[FieldName.SERVICE_AREA] == (
        "lives at 46005, in the area of the Valencia Ruzafa restaurant, in Valencia, not Barcelona Gràcia, the one "
        "they applied for"
    )


@pytest.mark.parametrize(
    ("code", "app"),
    [
        ("8001", BARCELONA),  # 08001 with its leading zero lost
        ("80011", BARCELONA),  # no Spanish province begins with 80
        ("00123", MADRID),
        ("6700", CDMX),  # 06700, Roma Norte, without its zero
    ],
)
def test_a_code_that_cant_exist_is_no_answer_and_says_a_spoken_zero_gets_lost(code: str, app: Application) -> None:
    state, result = save(NewAnswers(postal_code=code), app=app)
    assert state.answers.area is None and state.knockout is None  # unknown is not "no"
    assert result.not_saved[FieldName.SERVICE_AREA].startswith(f"{code!r} can't be a postal code in ")
    assert "08001 is heard as 8001" in result.not_saved[FieldName.SERVICE_AREA]


def test_the_same_postal_code_is_read_in_the_vacancy_country() -> None:
    _, in_mexico = save(NewAnswers(postal_code="44160"), app=CDMX)
    _, in_spain = save(NewAnswers(postal_code="44160"))
    assert in_mexico.knockout is None and "Guadalajara Americana" in in_mexico.saved[FieldName.SERVICE_AREA]
    assert in_spain.knockout is not None


@pytest.mark.parametrize(
    ("new", "hint"),
    [
        (NewAnswers(place="Centro"), "several of our areas"),
        (NewAnswers(place="Narnia"), "isn't among the places we know"),
        (NewAnswers(postal_code="280"), "can't be a postal code in Mexico (5 digits)"),
    ],
)
def test_unusable_area_answers_are_not_saved_and_say_why(new: NewAnswers, hint: str) -> None:
    state, result = save(new, app=CDMX)
    assert hint in result.not_saved[FieldName.SERVICE_AREA]
    assert state.answers.area is None and state.knockout is None  # unknown is not "no"


def test_a_place_miss_lists_the_known_places_and_the_other_answers_are_still_saved() -> None:
    state, result = save(NewAnswers(place="Zona Norte", availability=[Availability.WEEKENDS]))
    assert result.saved == {FieldName.AVAILABILITY: "weekends"}
    message = result.not_saved[FieldName.SERVICE_AREA]
    assert all(place in message for place in ("Madrid: Chamberí, Malasaña", "Getafe", "Barcelona: Gràcia"))
    assert "Roma Norte" not in message  # the vacancy's country only
    # Names only: with a postal code in sight the model could save one the candidate never gave.
    assert not any(prefix in message for location in CONFIG.locations for prefix in location.postal_prefixes)
    assert state.answers.area is None and state.knockout is None


def test_a_known_place_saves_the_matching_restaurant_even_if_not_the_applied_one() -> None:
    _, result = save(NewAnswers(place="Obispado"), app=CDMX)
    assert result.saved[FieldName.SERVICE_AREA] == (
        "lives at Obispado, in the area of the Monterrey Centro restaurant, in Monterrey, not CDMX Roma-Condesa, the "
        "one they applied for"
    )


def test_relative_dates_resolve_in_the_restaurant_time_zone() -> None:
    late_saturday_utc = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)  # Sunday in Madrid, Saturday in CDMX
    asap = NewAnswers(start_date=StartDateSpec(kind="asap"))
    madrid, _ = save(asap, now=late_saturday_utc)
    cdmx, _ = save(asap, app=CDMX, now=late_saturday_utc)
    assert (madrid.answers.start_date, cdmx.answers.start_date) == (date(2026, 10, 25), date(2026, 10, 24))


def test_experience_needs_platforms_unless_there_is_none() -> None:
    state, result = save(NewAnswers(experience_years=2))
    assert "platforms" in result.not_saved[FieldName.EXPERIENCE] and FieldName.EXPERIENCE in state.still_to_ask
    state, result = save(NewAnswers(platforms=["didi"]), state=state)
    assert result.saved[FieldName.EXPERIENCE] == "2 years: DiDi Food"
    state, _ = save(NewAnswers(experience_years=0))
    assert state.is_saved(FieldName.EXPERIENCE)


def test_saving_a_field_clears_it_from_unclear() -> None:
    state = in_progress()
    state.unclear[FieldName.SCHEDULE] = "kept changing"
    state, _ = save(NewAnswers(schedule=[Schedule.MORNING]), state=state)
    assert FieldName.SCHEDULE not in state.unclear


def test_knockout_corrected_after_the_outcome_reopens_with_a_review_flag() -> None:
    state, _ = save(NewAnswers(license=LicenseAnswer.NO))
    state, _ = finish_screening(state)
    assert state.outcome is Outcome.NOT_QUALIFIED

    state, result = save(NewAnswers(license=LicenseAnswer.YES), state=state)
    assert (state.status, state.outcome) == (Status.IN_PROGRESS, None)
    assert state.flags[0].reason is ReviewReason.KNOCKOUT_CHANGED
    assert FieldName.SERVICE_AREA in result.still_to_ask


def test_after_the_hiring_teams_decision_a_corrected_knockout_reopens_nothing() -> None:
    state, _ = finish_screening(save(NewAnswers(license=LicenseAnswer.NO))[0])
    state, result = save(NewAnswers(license=LicenseAnswer.YES), state=state, decision=Outcome.NOT_QUALIFIED)
    # Recorded for the recruiter's page as a review reason; the screening stays closed with nothing left to ask.
    assert (state.status, state.outcome) == (Status.COMPLETED, Outcome.NEEDS_HUMAN_REVIEW)
    assert state.flags[0].reason is ReviewReason.KNOCKOUT_CHANGED
    assert result.still_to_ask == [] and result.decision and result.decision.startswith("not_qualified")


def test_after_the_hiring_teams_decision_a_new_knockout_is_no_new_verdict() -> None:
    state = save(NewAnswers(full_name="Lucía Martín García"))[0]
    state.flags.append(ReviewFlag(reason=ReviewReason.HUMAN_REQUESTED, detail="prefers a call"))
    state, _ = finish_screening(state)
    state, result = save(NewAnswers(license=LicenseAnswer.NO), state=state, decision=Outcome.QUALIFIED)
    assert result.knockout is None and state.outcome is Outcome.NEEDS_HUMAN_REVIEW
    # Even if the agent asks for the outcome again, the decision is what it gets.
    _, finished = finish_screening(state, Outcome.QUALIFIED)
    assert (finished.outcome, finished.reasons) == (Outcome.QUALIFIED, ["the hiring team's decision, final"])


# --- finish_screening ---------------------------------------------------------------------------------------


def test_finish_refuses_while_answers_are_missing() -> None:
    state, result = finish_screening(save(NewAnswers(license=LicenseAnswer.YES))[0])
    assert not result.finished and state.status is Status.IN_PROGRESS
    assert result.still_to_ask[0] is FieldName.FULL_NAME


def test_a_person_requested_allows_finishing_early_for_review() -> None:
    state = save(NewAnswers(license=LicenseAnswer.YES))[0]
    state.flags.append(ReviewFlag(reason=ReviewReason.HUMAN_REQUESTED, detail="prefers a call"))
    state, result = finish_screening(state)
    assert (result.finished, result.outcome) == (True, Outcome.NEEDS_HUMAN_REVIEW)


def test_everything_saved_and_nothing_flagged_is_qualified() -> None:
    state, result = finish_screening(save(EVERYTHING)[0])
    assert (result.finished, result.outcome, state.status) == (True, Outcome.QUALIFIED, Status.COMPLETED)


def test_a_knockout_no_finishes_early_as_not_qualified() -> None:
    _, result = finish_screening(save(NewAnswers(license=LicenseAnswer.NO))[0])
    assert (result.outcome, result.reasons) == (Outcome.NOT_QUALIFIED, ["no_license"])


def test_unclear_required_field_or_a_person_requested_means_review() -> None:
    unclear = save(EVERYTHING.model_copy(update={"license": None}))[0]
    unclear.unclear[FieldName.LICENSE] = "license from Venezuela, validity unknown"
    _, result = finish_screening(unclear)
    assert result.outcome is Outcome.NEEDS_HUMAN_REVIEW and "license unclear" in result.reasons[0]

    asked = save(EVERYTHING)[0]
    asked.flags.append(ReviewFlag(reason=ReviewReason.HUMAN_REQUESTED, detail="wants to talk about pay"))
    assert finish_screening(asked)[1].outcome is Outcome.NEEDS_HUMAN_REVIEW


def test_unclear_optional_field_is_a_gap_and_still_qualified() -> None:
    state = save(EVERYTHING.model_copy(update={"schedule": None}))[0]
    state.unclear[FieldName.SCHEDULE] = "kept changing"
    _, result = finish_screening(state)
    assert (result.outcome, result.gaps) == (Outcome.QUALIFIED, [FieldName.SCHEDULE])


def test_asking_for_a_person_after_the_close_moves_the_outcome_to_review_once() -> None:
    state, _ = finish_screening(save(NewAnswers(license=LicenseAnswer.NO))[0])
    state = request_human(request_human(state, "wants to explain"), "asks again")
    assert state.outcome is Outcome.NEEDS_HUMAN_REVIEW
    assert [flag.reason for flag in state.flags] == [ReviewReason.HUMAN_REQUESTED]


def test_ending_for_conduct_closes_for_review_and_other_reasons_withdraw() -> None:
    state = end_conversation(in_progress(), EndReason.CONDUCT)
    assert (state.status, state.outcome) == (Status.COMPLETED, Outcome.NEEDS_HUMAN_REVIEW)
    state = end_conversation(in_progress(), EndReason.NOT_INTERESTED)
    assert (state.status, state.outcome) == (Status.WITHDRAWN, None)


def test_key_data_lists_every_field_and_every_reason_behind_the_outcome() -> None:
    state = save(NewAnswers(full_name="Lucía Martín García", license=LicenseAnswer.NO))[0]
    state.unclear[FieldName.SCHEDULE] = "kept changing"
    state, _ = finish_screening(state)
    data = key_data(request_human(state, "wants to explain"), CONFIG)
    assert list(data.answers) == list(FieldName)
    assert data.answers[FieldName.LICENSE] == "no"
    assert data.answers[FieldName.SCHEDULE] == "unclear: kept changing"
    assert data.answers[FieldName.START_DATE] == "not answered"
    # A person asked for after the knockout shows next to it, and moves the outcome to review.
    assert (data.outcome, data.reasons) == (
        Outcome.NEEDS_HUMAN_REVIEW,
        ["no_license", "human_requested: wants to explain"],
    )
