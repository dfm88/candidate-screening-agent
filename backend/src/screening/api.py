"""HTTP routes. The request, response and event models are in contract.py."""

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from screening import db
from screening.contract import (
    ApplicationCreated,
    ApplicationRequest,
    ApplyForm,
    CandidateMessage,
    ChatMessage,
    ChatSession,
    ChatStatus,
    MessageAccepted,
    Typing,
    Vacancy,
)
from screening.db import Candidate, Source
from screening.domain import Status
from screening.service import ScreeningService, application_of

router = APIRouter(prefix="/api")


def service_of(request: Request) -> ScreeningService:
    return request.app.state.service


Service = Annotated[ScreeningService, Depends(service_of)]


async def chat_candidate(token: str, service: Service) -> Candidate:
    # A dependency, so an unknown link is a 404 before an event stream starts.
    candidate = await service.find(token)
    if candidate is None:
        raise HTTPException(status_code=404, detail="This chat link isn't valid.")
    return candidate


ChatCandidate = Annotated[Candidate, Depends(chat_candidate)]


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
    """The public apply form. Recruiters ("add applicant") and the seed script use their own ways in."""
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
    last_event_id: Annotated[int | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:
    """The chat's messages since Last-Event-ID (all of them on first connect), then live events. Reconnects and
    redeploys lose nothing: every message is in the database before it's published."""
    # Subscribe before reading the database, so a message stored in between arrives live instead of being lost.
    with service.channel.subscribe(candidate.id) as live:
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
