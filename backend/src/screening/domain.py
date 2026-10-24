"""Screening domain: from what the candidate said to what happens next, deterministically.

The LLM fills a `TurnExtraction` with what the new messages *say*. `decide` validates it against the client
config, applies the 2-try policy, knockouts and review reasons (⚑), and returns the acts for the reply.
No model output sets a status or an outcome. Names follow docs/process-design.md.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from screening.config import ClientConfig, Country, Language, Locale, Location

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


FIELD_ORDER = tuple(FieldName)
KNOCKOUTS = frozenset({FieldName.LICENSE, FieldName.SERVICE_AREA})
REQUIRED = KNOCKOUTS | {FieldName.FULL_NAME}


class FieldStatus(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    UNCLEAR = "unclear"
    # Still unclear after the last try: a ⚑ for required fields, a gap for the others.
    UNRESOLVED = "unresolved"


class Availability(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    WEEKENDS = "weekends"


class Schedule(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    FLEXIBLE = "flexible"


class ReviewReason(StrEnum):
    UNRESOLVED_FIELD = "unresolved_field"
    HUMAN_REQUESTED = "human_requested"
    KNOCKOUT_CHANGED = "knockout_changed"
    CONVERSATION_ENDED = "conversation_ended"


class NotQualifiedReason(StrEnum):
    NO_LICENSE = "no_license"
    OUTSIDE_SERVICE_AREA = "outside_service_area"


class Flag(BaseModel):
    """A review reason (⚑)."""

    reason: ReviewReason
    field: FieldName | None = None
    detail: str | None = None


# --- Status transitions -----------------------------------------------------------------------------------

_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.INVITED, Status.IN_PROGRESS}),
    Status.INVITED: frozenset({Status.IN_PROGRESS, Status.ABANDONED, Status.WITHDRAWN}),
    Status.IN_PROGRESS: frozenset({Status.COMPLETED, Status.ABANDONED, Status.WITHDRAWN}),
    Status.ABANDONED: frozenset({Status.IN_PROGRESS, Status.WITHDRAWN}),
    # Reopened when a candidate corrects a knockout answer after hearing it disqualifies.
    Status.COMPLETED: frozenset({Status.IN_PROGRESS}),
    # A withdrawn candidate who writes again of their own accord can resume.
    Status.WITHDRAWN: frozenset({Status.IN_PROGRESS}),
}


def transition(current: Status, new: Status) -> Status:
    if new not in _TRANSITIONS[current]:
        raise ValueError(f"invalid status transition {current} -> {new}")
    return new


# --- What the candidate said (the extractor's output) -----------------------------------------------------


class LicenseAnswer(StrEnum):
    YES = "yes"
    NO = "no"
    UNCLEAR = "unclear"


class StartDateSpec(BaseModel):
    """The start date as the candidate expressed it; code turns it into a date in the restaurant's time zone."""

    kind: Literal["date", "in", "weekday", "asap", "unclear"] = Field(
        description="date: a calendar date; in: N days/weeks/months from now; weekday: next given weekday; "
        "asap: immediately; unclear: no usable date."
    )
    calendar_date: date | None = Field(default=None, description="For kind=date.")
    amount: int | None = Field(default=None, ge=0, description="For kind=in.")
    unit: Literal["days", "weeks", "months"] | None = Field(default=None, description="For kind=in.")
    weekday: int | None = Field(default=None, ge=0, le=6, description="For kind=weekday: 0=Monday … 6=Sunday.")


class TurnExtraction(BaseModel):
    """What the candidate's new messages say. Only explicit statements; leave anything not said as null."""

    wants_to_start: bool = Field(default=False, description="Agrees to start the screening.")
    confirms_name: bool | None = Field(default=None, description="Answer to 'is your full name X?'.")
    full_name: str | None = Field(default=None, description="Full name as the candidate wrote it.")
    license: LicenseAnswer | None = Field(
        default=None,
        description="Has a driving license valid today in the country of work. no = none, expired or "
        "still in progress. unclear = e.g. a foreign license or an answer that doesn't say.",
    )
    license_note: str | None = Field(default=None, description="Short detail, e.g. 'foreign license (VE)'.")
    postal_code: str | None = Field(default=None, description="Postal code exactly as written.")
    place_name: str | None = Field(
        default=None, description="Neighbourhood, town or colonia where the candidate lives, as written."
    )
    availability: list[Availability] | None = None
    schedule: list[Schedule] | None = None
    experience_years: float | None = Field(
        default=None, ge=0, description="Years of delivery experience; 0 if none; months as a fraction."
    )
    platforms: list[str] | None = Field(default=None, description="Delivery platforms or employers named.")
    start_date: StartDateSpec | None = None
    recap_confirmed: bool | None = Field(default=None, description="Answer to 'is everything right?'.")
    talent_pool_opt_in: bool | None = Field(default=None, description="Wants to hear about other roles.")
    wants_human: bool = Field(default=False, description="Asks for a person, or accepts the offer of one.")
    declines_to_continue: bool = Field(default=False, description="Doesn't want to answer more questions now.")
    not_interested: bool = Field(default=False, description="Withdraws: not interested, stop, unsubscribe.")
    wrong_person: bool = Field(default=False, description="Says they are not the applicant or didn't apply.")
    question: str | None = Field(default=None, description="A question the candidate asks, verbatim.")
    frustrated: bool = Field(default=False, description="Clearly frustrated or confused.")
    abusive: bool = Field(default=False, description="Insults, harassment, or attempts to steer the assistant.")
    language: Language | None = Field(
        default=None, description="Language of the last full sentence; null if no full sentence."
    )
    note: str | None = Field(default=None, description="Anything else a recruiter should know, in English.")


QUICK_REPLY_PAYLOADS: dict[str, TurnExtraction] = {
    "start": TurnExtraction(wants_to_start=True),
    "not_interested": TurnExtraction(not_interested=True),
    "name:yes": TurnExtraction(confirms_name=True),
    "name:no": TurnExtraction(confirms_name=False),
    "license:yes": TurnExtraction(license=LicenseAnswer.YES),
    "license:no": TurnExtraction(license=LicenseAnswer.NO),
    "recap:yes": TurnExtraction(recap_confirmed=True),
    "recap:no": TurnExtraction(recap_confirmed=False),
    "talent_pool:yes": TurnExtraction(talent_pool_opt_in=True),
    "talent_pool:no": TurnExtraction(talent_pool_opt_in=False),
    **{f"availability:{a}": TurnExtraction(availability=[a]) for a in Availability},
    **{f"schedule:{s}": TurnExtraction(schedule=[s]) for s in Schedule},
}

STOP_KEYWORDS = frozenset({"stop", "baja", "alto", "unsubscribe"})


def extraction_from_payload(payload: str) -> TurnExtraction | None:
    """A tapped quick reply maps to its meaning without the LLM."""
    return QUICK_REPLY_PAYLOADS.get(payload)


def is_stop_keyword(text: str) -> bool:
    return _normalize(text) in STOP_KEYWORDS


# --- What we know ------------------------------------------------------------------------------------------


class AreaMatch(BaseModel):
    location_id: str
    postal_code: str | None = None
    place: str | None = None


class Experience(BaseModel):
    years: float | None = None
    platforms: list[str] | None = None


class Answers(BaseModel):
    full_name: str | None = None
    has_license: bool | None = None
    license_note: str | None = None
    area: AreaMatch | None = None
    availability: list[Availability] = []
    schedule: list[Schedule] = []
    experience: Experience | None = None
    start_date: date | None = None


class FieldState(BaseModel):
    status: FieldStatus = FieldStatus.MISSING
    tries: int = 0


class Awaiting(StrEnum):
    START = "start"
    FIELD = "field"
    RECAP = "recap"
    TALENT_POOL = "talent_pool"
    NOTHING = "nothing"


class Application(BaseModel):
    """What the ATS sent about the applicant."""

    full_name: str
    vacancy_location_id: str

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0]


class ScreeningState(BaseModel):
    status: Status = Status.PENDING
    outcome: Outcome | None = None
    not_qualified_reason: NotQualifiedReason | None = None
    language: Language
    answers: Answers = Field(default_factory=Answers)
    fields: dict[FieldName, FieldState] = Field(default_factory=lambda: {f: FieldState() for f in FieldName})
    awaiting: Awaiting = Awaiting.START
    asking: FieldName | None = None
    flags: list[Flag] = []
    talent_pool_opt_in: bool | None = None
    human_requested: bool = False
    frustration_offered: bool = False
    simplify: bool = False
    abuse_strikes: int = 0
    open_questions: list[str] = []
    notes: list[str] = []

    def is_resolved(self, name: FieldName) -> bool:
        return self.fields[name].status in (FieldStatus.VALID, FieldStatus.UNRESOLVED)

    @property
    def gaps(self) -> list[FieldName]:
        return [f for f in FIELD_ORDER if f not in REQUIRED and self.fields[f].status is FieldStatus.UNRESOLVED]


# --- What to say next ----------------------------------------------------------------------------------


class QuickReply(BaseModel):
    label: str
    payload: str


class ResponderAct(BaseModel):
    """Something the responder LLM phrases. It never decides; code chose the act."""

    kind: Literal["ask", "ask_correction", "answer", "handoff", "offer_human", "redirect", "resume", "closed"]
    field: FieldName | None = None
    attempt: int = 1
    question: str | None = None


class Decision(BaseModel):
    state: ScreeningState
    locale: Locale
    acts: list[ResponderAct] = []
    # Fixed messages, already rendered; sent after the responder's bubbles.
    template_bubbles: list[str] = []
    quick_replies: list[QuickReply] = []


# --- Validation helpers ---------------------------------------------------------------------------------------

_PLACE_PREFIXES = ("colonia ", "col ", "barrio ", "barri ", "fraccionamiento ")


def _normalize(text: str) -> str:
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped.casefold()).strip()


def locations_in(config: ClientConfig, country: Country) -> list[Location]:
    return [loc for loc in config.locations if loc.country is country]


def match_postal_code(code: str, config: ClientConfig, country: Country, preferred: str) -> Location | None:
    """The restaurant whose area contains `code`, read in the vacancy's country (ES and MX both use 5 digits)."""
    matches = [
        (loc, len(prefix))
        for loc in locations_in(config, country)
        for prefix in loc.postal_prefixes
        if code.startswith(prefix)
    ]
    if not matches:
        return None
    for loc, _ in matches:
        if loc.id == preferred:
            return loc
    return max(matches, key=lambda m: m[1])[0]


def lookup_place(name: str, config: ClientConfig, country: Country) -> list[Location]:
    """Restaurants whose area contains a place with this name; more than one means 'ask for the postal code'."""
    wanted = _normalize(name)
    for prefix in _PLACE_PREFIXES:
        if wanted.startswith(prefix):
            wanted = wanted.removeprefix(prefix)
    found = [loc for loc in locations_in(config, country) if any(_normalize(place) == wanted for place in loc.places)]
    return list({loc.id: loc for loc in found}.values())


def normalize_platforms(names: list[str], config: ClientConfig) -> list[str]:
    canonical: list[str] = []
    for name in names:
        key = _normalize(name)
        match = next(
            (canon for canon, aliases in config.platforms.items() if key in {_normalize(a) for a in aliases}),
            name.strip(),
        )
        if match not in canonical:
            canonical.append(match)
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
            days = {"days": 1, "weeks": 7, "months": 30}[spec.unit]
            return today + timedelta(days=spec.amount * days)
        case "weekday" if spec.weekday is not None:
            # The next such weekday, never today: "el lunes" said on a Monday means next Monday.
            ahead = (spec.weekday - today.weekday() - 1) % 7 + 1
            return today + timedelta(days=ahead)
    return None


# --- The rules ---------------------------------------------------------------------------------------------


class _Turn:
    """Collects the reply for one turn while the rules run."""

    def __init__(self, state: ScreeningState, app: Application, config: ClientConfig, now: datetime) -> None:
        self.s = state
        self.app = app
        self.config = config
        self.location = config.locations_by_id[app.vacancy_location_id]
        self.today = now.astimezone(self.location.tz).date()
        self.acts: list[ResponderAct] = []
        self.bubbles: list[str] = []
        self.quick_replies: list[QuickReply] = []

    @property
    def locale(self) -> Locale:
        return Locale.of(self.s.language, self.location.country)

    def template(self, key: str, **params: str) -> None:
        defaults = {"first_name": self.app.first_name, "full_name": self.app.full_name, "city": self.location.city}
        self.bubbles += self.config.render(key, self.locale, **(defaults | params))

    def options(self, *pairs: tuple[str, str]) -> None:
        self.quick_replies = [QuickReply(label=self.config.label(self.locale, k), payload=p) for k, p in pairs]

    def decision(self) -> Decision:
        return Decision(
            state=self.s,
            locale=self.locale,
            acts=self.acts,
            template_bubbles=self.bubbles,
            quick_replies=self.quick_replies,
        )


def decide(
    state: ScreeningState, app: Application, ex: TurnExtraction, now: datetime, config: ClientConfig
) -> Decision:
    """Apply one batch of candidate messages to the screening and choose the reply."""
    t = _Turn(state.model_copy(deep=True), app, config, now)
    s = t.s
    if ex.language is not None:
        s.language = ex.language
    if ex.note:
        s.notes.append(ex.note)

    if ex.wrong_person or ex.not_interested:
        if s.status is not Status.WITHDRAWN:
            s.status = transition(s.status, Status.WITHDRAWN)
        s.awaiting = Awaiting.NOTHING
        t.template("wrong_person" if ex.wrong_person else "withdrawn")
        return t.decision()

    if s.status is Status.COMPLETED:
        return _after_close(t, ex)

    # Never started (even if it went silent or withdrew first): the intro carries the AI disclosure.
    if s.awaiting is Awaiting.START:
        # Without a first message from us (the candidate came through the link), the intro introduces us.
        direct = s.status is Status.PENDING
        s.status = transition(s.status, Status.IN_PROGRESS)
        _apply_answers(t, ex)
        _answer_side_requests(t, ex)
        s.awaiting, s.asking = Awaiting.FIELD, FieldName.FULL_NAME
        t.template("intro_direct" if direct else "intro")
        t.options(("yes", "name:yes"), ("no", "name:no"))
        return t.decision()

    if s.status in (Status.ABANDONED, Status.WITHDRAWN):
        s.status = transition(s.status, Status.IN_PROGRESS)
        t.acts.append(ResponderAct(kind="resume"))

    return _in_progress(t, ex)


def _in_progress(t: _Turn, ex: TurnExtraction) -> Decision:
    s = t.s
    if ex.abusive:
        s.abuse_strikes += 1
        if s.abuse_strikes >= 2:
            s.flags.append(Flag(reason=ReviewReason.CONVERSATION_ENDED))
            _complete(t, Outcome.NEEDS_HUMAN_REVIEW, template="close_ended")
            return t.decision()
        t.acts.append(ResponderAct(kind="redirect"))

    _answer_side_requests(t, ex)
    if ex.declines_to_continue and s.human_requested:
        _complete(t, Outcome.NEEDS_HUMAN_REVIEW, template="close_review")
        return t.decision()

    touched = _apply_answers(t, ex)
    if s.not_qualified_reason is not None:
        _close_not_qualified(t)
        return t.decision()

    if s.awaiting is Awaiting.RECAP:
        if touched:
            _send_recap(t)
        elif ex.recap_confirmed:
            _finalize(t)
        elif ex.recap_confirmed is False:
            t.acts.append(ResponderAct(kind="ask_correction"))
        else:
            t.options(("recap_ok", "recap:yes"), ("recap_fix", "recap:no"))
        return t.decision()

    if s.asking is not None and s.asking not in touched:
        _count_silent_try(t, ex)
    _ask_next(t)
    return t.decision()


def _answer_side_requests(t: _Turn, ex: TurnExtraction) -> None:
    """Questions, requests for a person and frustration are handled alongside the screening, not instead of it."""
    s = t.s
    if ex.question:
        s.open_questions.append(ex.question)
        t.acts.append(ResponderAct(kind="answer", question=ex.question))
    if ex.frustrated:
        s.simplify = True
        if not s.frustration_offered and not s.human_requested:
            s.frustration_offered = True
            t.acts.append(ResponderAct(kind="offer_human"))
    if ex.wants_human and not s.human_requested:
        s.human_requested = True
        s.flags.append(Flag(reason=ReviewReason.HUMAN_REQUESTED))
        t.acts.append(ResponderAct(kind="handoff"))


def _count_silent_try(t: _Turn, ex: TurnExtraction) -> None:
    """The asked field wasn't answered. Only a reply that says nothing else counts as a failed try."""
    s = t.s
    field = s.asking
    assert field is not None
    state = s.fields[field]
    # A single-word name is accepted once the candidate has been asked for the surname: mononyms exist.
    if field is FieldName.FULL_NAME and s.answers.full_name and state.tries >= 1:
        state.status = FieldStatus.VALID
        return
    said_something_else = bool(t.acts) or ex.question or ex.wants_human or ex.frustrated or ex.abusive
    if not said_something_else:
        _fail_try(t, field)


def _fail_try(t: _Turn, field: FieldName, detail: str | None = None) -> None:
    state = t.s.fields[field]
    state.tries += 1
    if state.tries < t.config.max_tries:
        state.status = FieldStatus.UNCLEAR
        return
    state.status = FieldStatus.UNRESOLVED
    if field in REQUIRED:
        t.s.flags.append(Flag(reason=ReviewReason.UNRESOLVED_FIELD, field=field, detail=detail))


def _valid(t: _Turn, field: FieldName) -> None:
    t.s.fields[field].status = FieldStatus.VALID


def _apply_answers(t: _Turn, ex: TurnExtraction) -> set[FieldName]:
    """Validate every field the messages mention, in any order. Returns the fields touched."""
    s, a, touched = t.s, t.s.answers, set[FieldName]()

    if ex.full_name or ex.confirms_name is not None:
        touched.add(FieldName.FULL_NAME)
        name = (ex.full_name or (t.app.full_name if ex.confirms_name else "")).strip()
        if not name:
            _fail_try(t, FieldName.FULL_NAME)
        else:
            a.full_name = name
            if len(name.split()) == 1 and s.fields[FieldName.FULL_NAME].tries == 0:
                _fail_try(t, FieldName.FULL_NAME)  # ask once for the surname(s)
            else:
                _valid(t, FieldName.FULL_NAME)

    if ex.license is not None:
        touched.add(FieldName.LICENSE)
        a.license_note = ex.license_note or a.license_note
        if ex.license is LicenseAnswer.UNCLEAR:
            _fail_try(t, FieldName.LICENSE, detail=ex.license_note)
        else:
            had_no = a.has_license is False
            a.has_license = ex.license is LicenseAnswer.YES
            _valid(t, FieldName.LICENSE)
            if not a.has_license:
                s.not_qualified_reason = NotQualifiedReason.NO_LICENSE
            elif had_no:
                _knockout_changed(t, FieldName.LICENSE)

    if ex.postal_code or ex.place_name:
        touched.add(FieldName.SERVICE_AREA)
        _apply_area(t, ex)

    if ex.availability:
        touched.add(FieldName.AVAILABILITY)
        a.availability = list(dict.fromkeys(ex.availability))
        _valid(t, FieldName.AVAILABILITY)

    if ex.schedule:
        touched.add(FieldName.SCHEDULE)
        a.schedule = list(dict.fromkeys(ex.schedule))
        _valid(t, FieldName.SCHEDULE)

    if ex.experience_years is not None or ex.platforms is not None:
        touched.add(FieldName.EXPERIENCE)
        _apply_experience(t, ex)

    if ex.start_date is not None:
        touched.add(FieldName.START_DATE)
        resolved = resolve_start_date(ex.start_date, t.today)
        if resolved is None:
            _fail_try(t, FieldName.START_DATE)
        else:
            a.start_date = resolved
            _valid(t, FieldName.START_DATE)

    if ex.talent_pool_opt_in is not None:
        s.talent_pool_opt_in = ex.talent_pool_opt_in
    return touched


def _apply_area(t: _Turn, ex: TurnExtraction) -> None:
    s, a = t.s, t.s.answers
    country, preferred = t.location.country, t.location.id
    if ex.postal_code:
        code = re.sub(r"\D", "", ex.postal_code)
        if len(code) != 5:
            _fail_try(t, FieldName.SERVICE_AREA)
            return
        matched = match_postal_code(code, t.config, country, preferred)
        if matched is None:
            a.area = None
            _valid(t, FieldName.SERVICE_AREA)
            s.not_qualified_reason = NotQualifiedReason.OUTSIDE_SERVICE_AREA
            return
        was_outside = s.not_qualified_reason is NotQualifiedReason.OUTSIDE_SERVICE_AREA
        a.area = AreaMatch(location_id=matched.id, postal_code=code, place=ex.place_name)
        _valid(t, FieldName.SERVICE_AREA)
        if was_outside:
            _knockout_changed(t, FieldName.SERVICE_AREA)
        return
    assert ex.place_name is not None
    found = lookup_place(ex.place_name, t.config, country)
    if len(found) == 1:
        was_outside = s.not_qualified_reason is NotQualifiedReason.OUTSIDE_SERVICE_AREA
        a.area = AreaMatch(location_id=found[0].id, place=ex.place_name)
        _valid(t, FieldName.SERVICE_AREA)
        if was_outside:
            _knockout_changed(t, FieldName.SERVICE_AREA)
    else:
        # Unknown or ambiguous place: not a "no". The next try asks for the postal code.
        _fail_try(t, FieldName.SERVICE_AREA, detail=ex.place_name)


def _apply_experience(t: _Turn, ex: TurnExtraction) -> None:
    a = t.s.answers
    current = a.experience or Experience()
    years = ex.experience_years if ex.experience_years is not None else current.years
    platforms = normalize_platforms(ex.platforms, t.config) if ex.platforms is not None else current.platforms
    a.experience = Experience(years=years, platforms=platforms)
    if years is not None and years > 50:
        _fail_try(t, FieldName.EXPERIENCE)
    elif years == 0 or (years is not None and platforms is not None):
        _valid(t, FieldName.EXPERIENCE)
    else:
        _fail_try(t, FieldName.EXPERIENCE)  # years or platforms still missing


def _knockout_changed(t: _Turn, field: FieldName) -> None:
    """A knockout answer flipped after the candidate heard it disqualifies: a person decides."""
    s = t.s
    s.not_qualified_reason = None
    s.flags.append(Flag(reason=ReviewReason.KNOCKOUT_CHANGED, field=field))
    if s.status is Status.COMPLETED:
        s.status = transition(s.status, Status.IN_PROGRESS)
        s.outcome = None
        s.awaiting = Awaiting.FIELD


def _ask_next(t: _Turn) -> None:
    s = t.s
    pending = next((f for f in FIELD_ORDER if not s.is_resolved(f)), None)
    if pending is None:
        _send_recap(t)
        return
    s.awaiting, s.asking = Awaiting.FIELD, pending
    t.acts.append(ResponderAct(kind="ask", field=pending, attempt=s.fields[pending].tries + 1))
    match pending:
        case FieldName.FULL_NAME:
            pass
        case FieldName.LICENSE:
            t.options(("yes", "license:yes"), ("no", "license:no"))
        case FieldName.AVAILABILITY:
            t.options(*((a.value, f"availability:{a}") for a in Availability))
        case FieldName.SCHEDULE:
            t.options(*((sch.value, f"schedule:{sch}") for sch in Schedule))


def _send_recap(t: _Turn) -> None:
    t.s.awaiting, t.s.asking = Awaiting.RECAP, None
    t.bubbles += [render_recap(t.s, t.app, t.config, t.locale)]
    t.options(("recap_ok", "recap:yes"), ("recap_fix", "recap:no"))


def _finalize(t: _Turn) -> None:
    if t.s.flags:
        _complete(t, Outcome.NEEDS_HUMAN_REVIEW, template="close_review")
    else:
        _complete(t, Outcome.QUALIFIED, template="close_qualified")


def _complete(t: _Turn, outcome: Outcome, template: str) -> None:
    s = t.s
    s.status = transition(s.status, Status.COMPLETED)
    s.outcome = outcome
    s.awaiting, s.asking = Awaiting.NOTHING, None
    t.acts = [act for act in t.acts if act.kind == "answer"]
    t.template(template)


def _close_not_qualified(t: _Turn) -> None:
    s = t.s
    no_license = s.not_qualified_reason is NotQualifiedReason.NO_LICENSE
    _complete(t, Outcome.NOT_QUALIFIED, template="close_not_qualified_" + ("license" if no_license else "area"))
    s.awaiting = Awaiting.TALENT_POOL
    t.options(("yes", "talent_pool:yes"), ("no", "talent_pool:no"))


def _after_close(t: _Turn, ex: TurnExtraction) -> Decision:
    """Messages after an outcome: corrections, requests for a person, the talent-pool answer, questions."""
    s = t.s
    if s.outcome is Outcome.NOT_QUALIFIED:
        _apply_answers(t, ex)
        if s.status is Status.IN_PROGRESS:  # a knockout answer changed: reopen and keep collecting
            _answer_side_requests(t, ex)
            _ask_next(t)
            return t.decision()
        if ex.wants_human:
            s.flags.append(Flag(reason=ReviewReason.HUMAN_REQUESTED))
            s.human_requested = True
            s.outcome = Outcome.NEEDS_HUMAN_REVIEW
            s.awaiting = Awaiting.NOTHING
            t.template("close_review")
            return t.decision()
        if s.awaiting is Awaiting.TALENT_POOL and ex.talent_pool_opt_in is not None:
            s.awaiting = Awaiting.NOTHING
            t.template("talent_pool_yes" if ex.talent_pool_opt_in else "talent_pool_no")
            return t.decision()
    if ex.question:
        s.open_questions.append(ex.question)
        t.acts.append(ResponderAct(kind="answer", question=ex.question))
    else:
        t.acts.append(ResponderAct(kind="closed"))
    return t.decision()


# --- Rendering what we know ---------------------------------------------------------------------------------


def render_recap(s: ScreeningState, app: Application, config: ClientConfig, locale: Locale) -> str:
    a = s.answers

    def label(key: str) -> str:
        return config.label(locale, key)

    def known(field: FieldName, text: str | None) -> str:
        return text if s.fields[field].status is FieldStatus.VALID and text else label("unknown")

    area = None
    if a.area:
        place = a.area.place or a.area.postal_code
        area = f"{place} · {config.locations_by_id[a.area.location_id].name}"
    return config.render(
        "recap",
        locale,
        full_name=known(FieldName.FULL_NAME, a.full_name),
        license=known(FieldName.LICENSE, label("yes") if a.has_license else None),
        area=known(FieldName.SERVICE_AREA, area),
        availability=known(FieldName.AVAILABILITY, ", ".join(label(x) for x in a.availability)),
        schedule=known(FieldName.SCHEDULE, ", ".join(label(x) for x in a.schedule)),
        experience=known(FieldName.EXPERIENCE, _format_experience(a.experience, config, locale)),
        start_date=known(FieldName.START_DATE, _format_date(a.start_date, config, locale)),
    )[0]


def _format_experience(exp: Experience | None, config: ClientConfig, locale: Locale) -> str | None:
    if exp is None or exp.years is None:
        return None
    if exp.years == 0:
        return config.label(locale, "no_experience")
    if exp.years < 1:
        duration = config.label(locale, "months").format(n=round(exp.years * 12))
    elif exp.years == 1:
        duration = config.label(locale, "one_year")
    else:
        n = f"{exp.years:g}" if locale is Locale.EN else f"{exp.years:g}".replace(".", ",")
        duration = config.label(locale, "years").format(n=n)
    return f"{duration} ({', '.join(exp.platforms)})" if exp.platforms else duration


def _format_date(value: date | None, config: ClientConfig, locale: Locale) -> str | None:
    if value is None:
        return None
    return config.label(locale, "date_format").format(
        weekday=config.label_list(locale, "weekday_names")[value.weekday()],
        day=value.day,
        month=config.label_list(locale, "month_names")[value.month - 1],
    )
