from datetime import UTC, date, datetime

import pytest

from screening.config import DEFAULT_CLIENT_CONFIG, Country, Language, load_client_config
from screening.domain import (
    Application,
    Availability,
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
    finish_screening,
    lookup_place,
    match_postal_code,
    normalize_platforms,
    resolve_start_date,
    save_answers,
    transition,
)

CONFIG = load_client_config(DEFAULT_CLIENT_CONFIG)
NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)  # a Saturday morning in Madrid
MADRID = Application(full_name="Lucía Martín García", vacancy_location_id="es-mad-chamberi", language=Language.ES)
CDMX = Application(full_name="José Hernández", vacancy_location_id="mx-cdmx-roma", language=Language.ES)
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


def save(new: NewAnswers, state: ScreeningState | None = None, app: Application = MADRID, now: datetime = NOW):
    return save_answers(state or in_progress(), new, app, CONFIG, now)


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
        FieldName.SERVICE_AREA: "Madrid Chamberí (28010)",
        FieldName.AVAILABILITY: "weekends",
    }
    assert result.still_to_ask == [FieldName.FULL_NAME, FieldName.SCHEDULE, FieldName.EXPERIENCE, FieldName.START_DATE]
    assert result.knockout is None and state.answers.area and state.answers.area.location_id == "es-mad-chamberi"


def test_postal_code_outside_every_area_is_a_knockout() -> None:
    state, result = save(NewAnswers(postal_code="28001"))
    assert state.knockout is NotQualifiedReason.OUTSIDE_SERVICE_AREA
    assert result.knockout and "finish_screening" in result.knockout


def test_the_same_postal_code_is_read_in_the_vacancy_country() -> None:
    _, in_mexico = save(NewAnswers(postal_code="44160"), app=CDMX)
    _, in_spain = save(NewAnswers(postal_code="44160"))
    assert in_mexico.saved[FieldName.SERVICE_AREA] == "Guadalajara Americana (44160)" and in_mexico.knockout is None
    assert in_spain.knockout is not None


@pytest.mark.parametrize(
    ("new", "hint"),
    [
        (NewAnswers(place="Centro"), "matches several areas"),
        (NewAnswers(place="Narnia"), "isn't a place we know"),
        (NewAnswers(postal_code="280"), "not a 5-digit postal code"),
    ],
)
def test_unusable_area_answers_are_not_saved_and_say_what_to_ask(new: NewAnswers, hint: str) -> None:
    state, result = save(new, app=CDMX)
    assert hint in result.not_saved[FieldName.SERVICE_AREA]
    assert state.answers.area is None and state.knockout is None  # unknown is not "no"


def test_a_known_place_saves_the_matching_restaurant_even_if_not_the_applied_one() -> None:
    _, result = save(NewAnswers(place="Obispado"), app=CDMX)
    assert result.saved[FieldName.SERVICE_AREA] == "Monterrey Centro (Obispado)"


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
