"""The screening facts code owns: what we store, service areas, dates in the restaurant's time zone, and the outcome.

The conversation itself is the agent's job (agent.py and prompts/). Names follow docs/process-design.md.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from screening.config import ClientConfig, Country, Language, Location

# --- Vocabulary -------------------------------------------------------------------------------------------


class Status(StrEnum):
    PENDING = "pending"
    INVITED = "invited"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    WITHDRAWN = "withdrawn"


class Outcome(StrEnum):
    QUALIFIED = "qualified"
    NOT_QUALIFIED = "not_qualified"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class FieldName(StrEnum):
    FULL_NAME = "full_name"
    LICENSE = "license"
    SERVICE_AREA = "service_area"
    AVAILABILITY = "availability"
    SCHEDULE = "schedule"
    EXPERIENCE = "experience"
    START_DATE = "start_date"


# The order the agent asks in: knockouts first.
FIELD_ORDER = tuple(FieldName)
REQUIRED = frozenset({FieldName.FULL_NAME, FieldName.LICENSE, FieldName.SERVICE_AREA})


class LicenseAnswer(StrEnum):
    YES = "yes"
    NO = "no"


class Availability(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    WEEKENDS = "weekends"


class Schedule(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    FLEXIBLE = "flexible"


class NotQualifiedReason(StrEnum):
    NO_LICENSE = "no_license"
    OUTSIDE_SERVICE_AREA = "outside_service_area"


class ReviewReason(StrEnum):
    HUMAN_REQUESTED = "human_requested"
    KNOCKOUT_CHANGED = "knockout_changed"
    CONDUCT = "conduct"


class EndReason(StrEnum):
    NOT_INTERESTED = "not_interested"
    WRONG_PERSON = "wrong_person"
    CONDUCT = "conduct"


_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.INVITED, Status.IN_PROGRESS}),
    Status.INVITED: frozenset({Status.IN_PROGRESS, Status.ABANDONED, Status.WITHDRAWN}),
    Status.IN_PROGRESS: frozenset({Status.COMPLETED, Status.ABANDONED, Status.WITHDRAWN}),
    Status.ABANDONED: frozenset({Status.IN_PROGRESS, Status.WITHDRAWN}),
    # Reopened when a candidate corrects a knockout answer after hearing it disqualifies; or withdraws later.
    Status.COMPLETED: frozenset({Status.IN_PROGRESS, Status.WITHDRAWN}),
    Status.WITHDRAWN: frozenset(),
}


def transition(current: Status, new: Status) -> Status:
    if new not in _TRANSITIONS[current]:
        raise ValueError(f"invalid status transition {current} -> {new}")
    return new


# --- What the agent saves -----------------------------------------------------------------------------------


class StartDateSpec(BaseModel):
    """The start date as the candidate said it. Code turns it into a date in the restaurant's time zone."""

    kind: Literal["asap", "date", "in", "weekday"] = Field(
        description="asap: right away; date: a calendar date; in: N days/weeks/months from today; "
        "weekday: the next given weekday."
    )
    calendar_date: date | None = Field(default=None, description="For kind=date.")
    amount: int | None = Field(default=None, ge=0, description="For kind=in.")
    unit: Literal["days", "weeks", "months"] | None = Field(default=None, description="For kind=in.")
    weekday: int | None = Field(default=None, ge=0, le=6, description="For kind=weekday: 0=Monday … 6=Sunday.")


class NewAnswers(BaseModel):
    """Answers the candidate gave. Only what they said explicitly; leave everything else out."""

    full_name: str | None = Field(default=None, description="Full name as the candidate wrote or confirmed it.")
    license: LicenseAnswer | None = Field(
        default=None,
        description="yes: a license valid today for driving in the country of work. "
        "no: none, expired or still in progress.",
    )
    license_note: str | None = Field(default=None, description="Short detail, e.g. 'motorbike A2' or 'from Venezuela'.")
    postal_code: str | None = Field(default=None, description="Postal code where the candidate lives, as written.")
    place: str | None = Field(
        default=None, description="Neighbourhood, town or colonia where the candidate lives, as written."
    )
    availability: list[Availability] | None = None
    schedule: list[Schedule] | None = None
    experience_years: float | None = Field(
        default=None, ge=0, description="Years of delivery experience: 0 if none, months as a fraction."
    )
    platforms: list[str] | None = Field(default=None, description="Delivery platforms or companies they worked for.")
    start_date: StartDateSpec | None = None
    talent_pool_opt_in: bool | None = Field(
        default=None, description="After a 'not qualified' close: wants to hear about other roles."
    )
    candidate_language: Language | None = Field(
        default=None, description="Only when the candidate switches language: the language they now write in."
    )


class AreaMatch(BaseModel):
    # None: the postal code is outside every service area (a knockout).
    location_id: str | None
    postal_code: str | None = None
    place: str | None = None


class Experience(BaseModel):
    years: float | None = None
    platforms: list[str] | None = None

    @property
    def complete(self) -> bool:
        return self.years == 0 or (self.years is not None and self.platforms is not None)


class Answers(BaseModel):
    full_name: str | None = None
    license: LicenseAnswer | None = None
    license_note: str | None = None
    area: AreaMatch | None = None
    availability: list[Availability] = []
    schedule: list[Schedule] = []
    experience: Experience | None = None
    start_date: date | None = None


class ReviewFlag(BaseModel):
    """A review reason (⚑) recorded during the conversation. Unclear required fields are review reasons too."""

    reason: ReviewReason
    detail: str | None = None


class Application(BaseModel):
    """What the ATS sent about the applicant."""

    full_name: str
    vacancy_location_id: str
    language: Language

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0]


class ScreeningState(BaseModel):
    status: Status = Status.PENDING
    outcome: Outcome | None = None
    language: Language
    answers: Answers = Field(default_factory=Answers)
    # Fields the agent gave up on after two tries, with its note on why.
    unclear: dict[FieldName, str] = {}
    flags: list[ReviewFlag] = []
    open_questions: list[str] = []
    talent_pool_opt_in: bool | None = None

    def is_saved(self, field: FieldName) -> bool:
        answers = self.answers
        match field:
            case FieldName.FULL_NAME:
                return answers.full_name is not None
            case FieldName.LICENSE:
                return answers.license is not None
            case FieldName.SERVICE_AREA:
                return answers.area is not None
            case FieldName.AVAILABILITY:
                return bool(answers.availability)
            case FieldName.SCHEDULE:
                return bool(answers.schedule)
            case FieldName.EXPERIENCE:
                return answers.experience is not None and answers.experience.complete
            case FieldName.START_DATE:
                return answers.start_date is not None

    @property
    def still_to_ask(self) -> list[FieldName]:
        return [field for field in FIELD_ORDER if not self.is_saved(field) and field not in self.unclear]

    @property
    def knockout(self) -> NotQualifiedReason | None:
        if self.answers.license is LicenseAnswer.NO:
            return NotQualifiedReason.NO_LICENSE
        if self.answers.area is not None and self.answers.area.location_id is None:
            return NotQualifiedReason.OUTSIDE_SERVICE_AREA
        return None

    @property
    def gaps(self) -> list[FieldName]:
        return [field for field in self.unclear if field not in REQUIRED]


def describe_saved(state: ScreeningState, config: ClientConfig) -> dict[FieldName, str]:
    """Saved answers as short readable values, for the agent's context and for tool results."""
    answers = state.answers
    described: dict[FieldName, str] = {}
    if answers.full_name:
        described[FieldName.FULL_NAME] = answers.full_name
    if answers.license:
        note = f" ({answers.license_note})" if answers.license_note else ""
        described[FieldName.LICENSE] = f"{answers.license}{note}"
    if answers.area:
        where = answers.area.postal_code or answers.area.place
        if answers.area.location_id is None:
            described[FieldName.SERVICE_AREA] = f"outside every service area ({where})"
        else:
            described[FieldName.SERVICE_AREA] = f"{config.locations_by_id[answers.area.location_id].name} ({where})"
    if answers.availability:
        described[FieldName.AVAILABILITY] = ", ".join(answers.availability)
    if answers.schedule:
        described[FieldName.SCHEDULE] = ", ".join(answers.schedule)
    if answers.experience:
        years = "years unknown" if answers.experience.years is None else f"{answers.experience.years:g} years"
        platforms = answers.experience.platforms
        described[FieldName.EXPERIENCE] = years + (f": {', '.join(platforms)}" if platforms else "")
    if answers.start_date:
        described[FieldName.START_DATE] = answers.start_date.strftime("%Y-%m-%d (%A)")
    return described


# --- Saving answers -------------------------------------------------------------------------------------------


class SaveResult(BaseModel):
    saved: dict[FieldName, str]
    not_saved: dict[FieldName, str] = Field(description="Why each answer wasn't saved, and what to ask instead.")
    knockout: str | None = Field(description="Set when the candidate doesn't qualify: call finish_screening.")
    still_to_ask: list[FieldName]


def save_answers(
    state: ScreeningState, new: NewAnswers, application: Application, config: ClientConfig, now: datetime
) -> tuple[ScreeningState, SaveResult]:
    """Validate what we own (service areas, dates), save the answers and report back to the agent."""
    state = state.model_copy(deep=True)
    answers = state.answers
    vacancy = config.locations_by_id[application.vacancy_location_id]
    knockout_before = state.knockout
    touched: set[FieldName] = set()
    not_saved: dict[FieldName, str] = {}

    if new.full_name:
        answers.full_name = new.full_name.strip()
        touched.add(FieldName.FULL_NAME)

    if new.license is not None:
        answers.license = new.license
        answers.license_note = new.license_note or answers.license_note
        touched.add(FieldName.LICENSE)

    if new.postal_code:
        code = re.sub(r"\D", "", new.postal_code)
        if len(code) != 5:
            not_saved[FieldName.SERVICE_AREA] = f"{new.postal_code!r} is not a 5-digit postal code: ask again"
        else:
            # Spain and Mexico both use 5-digit codes: read the code in the vacancy's country.
            location = match_postal_code(code, config, vacancy.country, preferred=vacancy.id)
            answers.area = AreaMatch(location_id=location.id if location else None, postal_code=code, place=new.place)
            touched.add(FieldName.SERVICE_AREA)
    elif new.place:
        locations = lookup_place(new.place, config, vacancy.country)
        if len(locations) == 1:
            answers.area = AreaMatch(location_id=locations[0].id, place=new.place)
            touched.add(FieldName.SERVICE_AREA)
        elif locations:
            names = ", ".join(location.name for location in locations)
            not_saved[FieldName.SERVICE_AREA] = (
                f"{new.place!r} matches several areas ({names}): ask for the postal code"
            )
        else:
            # An unknown place isn't a "no": only the postal code can tell.
            not_saved[FieldName.SERVICE_AREA] = f"{new.place!r} isn't a place we know: ask for the postal code"

    if new.availability:
        answers.availability = list(dict.fromkeys(new.availability))
        touched.add(FieldName.AVAILABILITY)

    if new.schedule:
        answers.schedule = list(dict.fromkeys(new.schedule))
        touched.add(FieldName.SCHEDULE)

    if new.experience_years is not None or new.platforms is not None:
        current = answers.experience or Experience()
        answers.experience = Experience(
            years=new.experience_years if new.experience_years is not None else current.years,
            platforms=normalize_platforms(new.platforms, config) if new.platforms is not None else current.platforms,
        )
        touched.add(FieldName.EXPERIENCE)
        if not answers.experience.complete:
            missing = "the platforms or companies" if answers.experience.years else "how many years"
            not_saved[FieldName.EXPERIENCE] = f"partly saved: also ask {missing}"

    if new.start_date:
        today = now.astimezone(vacancy.tz).date()
        resolved = resolve_start_date(new.start_date, today)
        if resolved is None:
            not_saved[FieldName.START_DATE] = "that isn't a usable date: ask for an approximate date"
        else:
            answers.start_date = resolved
            touched.add(FieldName.START_DATE)

    if new.talent_pool_opt_in is not None:
        state.talent_pool_opt_in = new.talent_pool_opt_in
    if new.candidate_language is not None:
        state.language = new.candidate_language
    for field in touched:
        state.unclear.pop(field, None)

    # A knockout answer that changes after the outcome was given goes to a person.
    knockout_after = state.knockout
    if state.status is Status.COMPLETED and knockout_after != knockout_before:
        state.flags.append(ReviewFlag(reason=ReviewReason.KNOCKOUT_CHANGED, detail=f"was {knockout_before or 'none'}"))
        if knockout_after is None:  # no longer disqualified: reopen and collect the rest
            state.status = transition(state.status, Status.IN_PROGRESS)
            state.outcome = None
        else:
            state.outcome = Outcome.NEEDS_HUMAN_REVIEW

    described = describe_saved(state, config)
    return state, SaveResult(
        saved={field: described[field] for field in touched if field in described},
        not_saved=not_saved,
        knockout=f"{knockout_after}: the candidate doesn't qualify, call finish_screening" if knockout_after else None,
        still_to_ask=state.still_to_ask,
    )


# --- The outcome ------------------------------------------------------------------------------------------------


def compute_outcome(state: ScreeningState) -> tuple[Outcome, list[str]]:
    """A knockout "no" → not qualified; an unclear required field or a review flag → review; else qualified."""
    if state.knockout:
        return Outcome.NOT_QUALIFIED, [state.knockout]
    reasons = [f"{field} unclear: {note}" for field, note in state.unclear.items() if field in REQUIRED]
    reasons += [f"{flag.reason}: {flag.detail}" if flag.detail else flag.reason for flag in state.flags]
    return (Outcome.NEEDS_HUMAN_REVIEW if reasons else Outcome.QUALIFIED), reasons


class FinishResult(BaseModel):
    finished: bool
    outcome: Outcome | None
    reasons: list[str] = []
    gaps: list[FieldName] = Field(default=[], description="Optional fields left unclear: noted for the recruiter.")
    still_to_ask: list[FieldName] = Field(default=[], description="Ask these before finishing.")


def finish_screening(state: ScreeningState) -> tuple[ScreeningState, FinishResult]:
    state = state.model_copy(deep=True)
    if state.status is Status.COMPLETED:
        _, reasons = compute_outcome(state)
        return state, FinishResult(finished=True, outcome=state.outcome, reasons=reasons, gaps=state.gaps)
    # Code owns completeness: the agent can't close early unless a knockout already decided it, or the
    # candidate asked for a person (who then gets the partial screening).
    asked_for_person = any(flag.reason is ReviewReason.HUMAN_REQUESTED for flag in state.flags)
    if state.knockout is None and not asked_for_person and state.still_to_ask:
        return state, FinishResult(finished=False, outcome=None, still_to_ask=state.still_to_ask)
    outcome, reasons = compute_outcome(state)
    state.status = transition(state.status, Status.COMPLETED)
    state.outcome = outcome
    return state, FinishResult(finished=True, outcome=outcome, reasons=reasons, gaps=state.gaps)


def request_human(state: ScreeningState, detail: str) -> ScreeningState:
    """The candidate asks for a person. After the close, that moves any outcome to review."""
    state = state.model_copy(deep=True)
    if not any(flag.reason is ReviewReason.HUMAN_REQUESTED for flag in state.flags):
        state.flags.append(ReviewFlag(reason=ReviewReason.HUMAN_REQUESTED, detail=detail))
    if state.status is Status.COMPLETED:
        state.outcome = Outcome.NEEDS_HUMAN_REVIEW
    return state


def end_conversation(state: ScreeningState, reason: EndReason) -> ScreeningState:
    """Abuse closes the screening for a person to review; any other reason withdraws it, with no outcome change."""
    state = state.model_copy(deep=True)
    if reason is EndReason.CONDUCT:
        if not any(flag.reason is ReviewReason.CONDUCT for flag in state.flags):
            state.flags.append(ReviewFlag(reason=ReviewReason.CONDUCT))
        if state.status is not Status.COMPLETED:
            state.status = transition(state.status, Status.COMPLETED)
        state.outcome = Outcome.NEEDS_HUMAN_REVIEW
    elif state.status is not Status.WITHDRAWN:
        state.status = transition(state.status, Status.WITHDRAWN)
    return state


# --- Lookups ---------------------------------------------------------------------------------------------------

_PLACE_PREFIXES = ("colonia ", "col ", "barrio ", "barri ", "fraccionamiento ")


def _normalize(text: str) -> str:
    without_accents = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", without_accents.casefold()).strip()


def match_postal_code(code: str, config: ClientConfig, country: Country, preferred: str) -> Location | None:
    """The restaurant whose area contains `code`: the applied-to one if it serves it, else the longest prefix."""
    matches = [
        (location, len(prefix))
        for location in config.locations
        if location.country is country
        for prefix in location.postal_prefixes
        if code.startswith(prefix)
    ]
    if not matches:
        return None
    preferred_match = next((location for location, _ in matches if location.id == preferred), None)
    return preferred_match or max(matches, key=lambda match: match[1])[0]


def lookup_place(name: str, config: ClientConfig, country: Country) -> list[Location]:
    """Restaurants with a place of this name in their area. More than one means: ask for the postal code."""
    wanted = _normalize(name)
    for prefix in _PLACE_PREFIXES:
        wanted = wanted.removeprefix(prefix)
    return [
        location
        for location in config.locations
        if location.country is country and any(_normalize(place) == wanted for place in location.places)
    ]


def normalize_platforms(names: list[str], config: ClientConfig) -> list[str]:
    """Known platforms get their canonical name; anything else is kept as given."""
    canonical: list[str] = []
    for name in names:
        key = _normalize(name)
        known = next(
            (platform for platform, aliases in config.platforms.items() if key in {_normalize(a) for a in aliases}),
            name.strip(),
        )
        if known not in canonical:
            canonical.append(known)
    return canonical


def resolve_start_date(spec: StartDateSpec, today: date) -> date | None:
    """`today` is the date in the restaurant's time zone, so 'mañana' means tomorrow where the candidate works."""
    match spec.kind:
        case "asap":
            return today
        case "date" if spec.calendar_date is not None:
            if spec.calendar_date >= today:
                return spec.calendar_date
            # "15 de enero" said in October means next January.
            next_year = spec.calendar_date.replace(year=spec.calendar_date.year + 1)
            return next_year if next_year >= today else None
        case "in" if spec.amount is not None and spec.unit is not None:
            return today + timedelta(days=spec.amount * {"days": 1, "weeks": 7, "months": 30}[spec.unit])
        case "weekday" if spec.weekday is not None:
            # The next such weekday, never today: "el lunes" said on a Monday means next Monday.
            return today + timedelta(days=(spec.weekday - today.weekday() - 1) % 7 + 1)
    return None
