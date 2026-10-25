"""HTTP routes. The request, response and event models are in contract.py."""

import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from screening import db
from screening.agent import RecruiterSummary
from screening.channels import WebChatChannel
from screening.contract import (
    ApplicationCreated,
    ApplicationRequest,
    ApplyForm,
    CandidateDetail,
    CandidateList,
    CandidateMessage,
    CandidateRow,
    ChatMessage,
    ChatSession,
    ChatStatus,
    Decision,
    DecisionRequest,
    FieldMark,
    Login,
    MessageAccepted,
    Recruiter,
    Tab,
    TranscriptMessage,
    Typing,
    Vacancy,
)
from screening.db import Candidate, Source
from screening.domain import FIELD_ORDER, REQUIRED, FieldName, ScreeningState, Status, compute_outcome, key_data
from screening.service import ScreeningService, application_of

router = APIRouter(prefix="/api")


def service_of(request: Request) -> ScreeningService:
    return request.app.state.service


Service = Annotated[ScreeningService, Depends(service_of)]


def web_chat_of(request: Request) -> WebChatChannel:
    # These routes are the web chat's adapter, so they use its streams; the service only knows the Channel Protocol.
    return request.app.state.web_chat


WebChat = Annotated[WebChatChannel, Depends(web_chat_of)]


async def chat_candidate(token: str, service: Service) -> Candidate:
    # A dependency, so an unknown link is a 404 before an event stream starts.
    candidate = await service.find(token)
    if candidate is None:
        raise HTTPException(status_code=404, detail="This chat link isn't valid.")
    return candidate


ChatCandidate = Annotated[Candidate, Depends(chat_candidate)]


def recruiter_name(request: Request, service: Service) -> str:
    """The guard on every recruiter route: the name the login stored in the signed session cookie."""
    if service.settings.recruiter_password is None:
        raise HTTPException(status_code=503, detail="The recruiter tool isn't set up: set RECRUITER_PASSWORD.")
    name = request.session.get("recruiter")
    if name is None:
        raise HTTPException(status_code=401, detail="Log in first.")
    return name


RecruiterName = Annotated[str, Depends(recruiter_name)]
# Every route on it needs the login, so a new recruiter route can't forget the guard.
recruiter_router = APIRouter(prefix="/api/recruiter", dependencies=[Depends(recruiter_name)])


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    try:
        await db.ping(request.app.state.engine)
    except Exception:
        return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})


@router.get("/apply-form")
async def apply_form(service: Service) -> ApplyForm:
    config = service.config
    return ApplyForm(
        client_name=config.client_name,
        vacancies=[Vacancy.model_validate(location, from_attributes=True) for location in config.locations],
        access_code_required=service.settings.apply_access_code is not None,
    )


@router.post("/applications", status_code=201)
async def create_application(body: ApplicationRequest, service: Service) -> ApplicationCreated:
    """The public apply form. The seed script uses its own way in."""
    expected = service.settings.apply_access_code
    if expected is not None and not secrets.compare_digest((body.access_code or "").encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="That access code isn't right. Check the link you were given.")
    if body.vacancy_location_id not in service.config.locations_by_id:
        raise HTTPException(status_code=422, detail="Unknown vacancy.")
    candidate = await service.apply(
        # Namespaced and unguessable: a known id can't fetch someone else's chat link.
        external_id=f"apply-form:{body.submission_id}",
        source=Source.APPLY_FORM,
        full_name=body.full_name,
        phone=body.phone,
        vacancy_location_id=body.vacancy_location_id,
        language=body.language,
        whatsapp_opt_in=body.whatsapp_opt_in,
    )
    return ApplicationCreated(candidate_id=candidate.id, chat_url=f"/chat/{candidate.chat_token}")


@router.get("/chat/{token}")
async def chat_session(candidate: ChatCandidate, service: Service) -> ChatSession:
    return ChatSession(
        persona_name=service.config.persona_name,
        client_name=service.config.client_name,
        first_name=application_of(candidate).first_name,
        language=candidate.language,
        status=candidate.status,
    )


@router.post("/chat/{token}/messages", status_code=202)
async def send_message(body: CandidateMessage, candidate: ChatCandidate, service: Service) -> MessageAccepted:
    if candidate.status is Status.WITHDRAWN:
        raise HTTPException(status_code=409, detail="This chat is closed.")
    message = await service.receive(candidate, body.text, body.client_id)
    return MessageAccepted(id=message.id)


@router.get("/chat/{token}/events", response_class=EventSourceResponse)
async def chat_events(
    candidate: ChatCandidate,
    service: Service,
    web_chat: WebChat,
    last_event_id: Annotated[int | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:
    """The chat's messages since Last-Event-ID (all of them on first connect), then live events. Reconnects and
    redeploys lose nothing: every message is in the database before it's published."""
    # Subscribe before reading the database, so a message stored in between arrives live instead of being lost.
    with web_chat.subscribe(candidate.id) as live:
        sent = last_event_id or 0
        for message in await service.messages_after(candidate.id, sent):
            yield ServerSentEvent(event="message", id=str(message.id), data=message)
            sent = message.id
        yield ServerSentEvent(event="status", data=ChatStatus(status=candidate.status))
        while True:
            event = await live.get()
            if isinstance(event, ChatMessage):
                if event.id <= sent:
                    continue
                sent = event.id
                yield ServerSentEvent(event="message", id=str(event.id), data=event)
            elif isinstance(event, Typing):
                yield ServerSentEvent(event="typing", data=event)
            else:
                yield ServerSentEvent(event="status", data=event)


# --- Recruiter tool -----------------------------------------------------------------------------------------------


@router.post("/recruiter/login")
async def login(body: Login, request: Request, service: Service) -> Recruiter:
    expected = service.settings.recruiter_password
    if expected is None:
        raise HTTPException(status_code=503, detail="The recruiter tool isn't set up: set RECRUITER_PASSWORD.")
    if not secrets.compare_digest(body.password.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="That password isn't right.")
    # Attribution, not identity: with one shared password, the name is whatever the recruiter types (README).
    request.session["recruiter"] = body.name
    return Recruiter(name=body.name)


@recruiter_router.post("/logout", status_code=204)
async def logout(request: Request) -> None:
    request.session.clear()


@recruiter_router.get("/me")
async def me(name: RecruiterName) -> Recruiter:
    return Recruiter(name=name)


@recruiter_router.get("/candidates")
async def list_candidates(service: Service, tab: Tab | None = None, q: str | None = None) -> CandidateList:
    rows, counts = await service.candidates(tab, q.strip() if q else None)
    locations = service.config.locations_by_id
    return CandidateList(
        candidates=[
            CandidateRow(
                id=candidate.id,
                full_name=candidate.full_name,
                vacancy=locations[candidate.vacancy_location_id].name,
                country=locations[candidate.vacancy_location_id].country,
                status=candidate.status,
                outcome=candidate.outcome,
                decided_outcome=candidate.decided_outcome,
                reasons=compute_outcome(ScreeningState.model_validate(candidate.state))[1],
                tab=candidate_tab,
                created_at=candidate.created_at,
            )
            for candidate, candidate_tab in rows
        ],
        counts={tab: counts.get(tab, 0) for tab in Tab},
    )


@recruiter_router.get("/candidates/{candidate_id}")
async def candidate_detail(candidate_id: uuid.UUID, service: Service) -> CandidateDetail:
    found = await service.candidate(candidate_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No such candidate.")
    candidate, tab, messages = found
    config = service.config
    state = ScreeningState.model_validate(candidate.state)
    marks: dict[FieldName, FieldMark] = {}
    for field in FIELD_ORDER:
        if state.is_saved(field):
            marks[field] = FieldMark.SAVED
        elif field in state.unclear:
            marks[field] = FieldMark.REVIEW if field in REQUIRED else FieldMark.GAP
        else:
            marks[field] = FieldMark.MISSING
    area = state.answers.area
    matched = config.locations_by_id[area.location_id] if area and area.location_id else None
    decision = None
    if candidate.decided_outcome and candidate.decided_by and candidate.decided_at:
        decision = Decision(
            outcome=candidate.decided_outcome,
            by=candidate.decided_by,
            note=candidate.decision_note,
            at=candidate.decided_at,
        )
    return CandidateDetail(
        id=candidate.id,
        full_name=candidate.full_name,
        phone=candidate.phone,
        vacancy=Vacancy.model_validate(config.locations_by_id[candidate.vacancy_location_id], from_attributes=True),
        language=candidate.language,
        whatsapp_opt_in=candidate.whatsapp_opt_in,
        status=candidate.status,
        tab=tab,
        created_at=candidate.created_at,
        completed_at=candidate.completed_at,
        key_data=key_data(state, config),
        marks=marks,
        matched_location=Vacancy.model_validate(matched, from_attributes=True) if matched else None,
        summary=RecruiterSummary.model_validate(candidate.summary) if candidate.summary else None,
        decision=decision,
        chat_url=f"/chat/{candidate.chat_token}",
        transcript=[TranscriptMessage.model_validate(message) for message in messages],
    )


@recruiter_router.post("/candidates/{candidate_id}/decision")
async def decide(candidate_id: uuid.UUID, body: DecisionRequest, name: RecruiterName, service: Service) -> Decision:
    found = await service.candidate(candidate_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No such candidate.")
    candidate = found[0]
    if candidate.decided_outcome is not None:
        raise HTTPException(status_code=409, detail=f"{candidate.decided_by} already decided.")
    if candidate.status is not Status.COMPLETED:
        raise HTTPException(status_code=409, detail="The screening hasn't finished.")
    decided = await service.decide(candidate_id, body.outcome, body.note, name)
    if decided is None or decided.decided_at is None:
        # Another recruiter decided in between.
        raise HTTPException(status_code=409, detail="Someone else decided just now. Reload the page.")
    return Decision(outcome=body.outcome, by=name, note=body.note, at=decided.decided_at)
