"""Live voice calls through ElevenLabs Speech Engine. ElevenLabs runs speech-to-text, turn-taking, interruptions and
speech, and for each turn sends the call's transcript to our WebSocket; our turn answers it with the same agent, tools
and state as the text chat (`ScreeningService.voice_turn`), and its reply streams back to be spoken.

The rule kept here: an answer is only ever read against a question the candidate heard. ElevenLabs' transcript cuts
our lines to what was played, so the model reads the call from it, and the stored record is kept to it. A turn cut
off by a newer transcript, or by the end of the call, is discarded whole, like a failed text turn: the next turn reads
the same words, and what was heard of it, from the transcript. A stored turn whose reply was cut while it played is
corrected, on each new transcript and from ElevenLabs' history of the call when it ends. What no turn answered when
the call ends is answered in the chat.

Create or update the engines (one per language, from the client config), then put the ids it prints in the env:

    uv run --env-file .env python -m screening.voice wss://HOST/api/voice/ws
"""

import asyncio
import json
import re
import secrets
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field

import logfire
from elevenlabs import (
    AsrConversationalConfig,
    AsyncElevenLabs,
    ConvAiDynamicVariable,
    PrivacyConfigInput,
    SpeechEngineConfig,
    TtsConversationalConfigInput,
)
from elevenlabs.speech_engine import ConversationMessage, SpeechEngineSession, verify_speech_engine_jwt
from fastapi import APIRouter, HTTPException, Request, WebSocket

from screening.api import ChatCandidate, Service
from screening.config import DEFAULT_CLIENT_CONFIG, SERVICE_NAME, ClientConfig, Language, Settings, load_client_config
from screening.contract import Author, VoiceSession
from screening.domain import ScreeningState, Status
from screening.service import Correction, ScreeningService, StoredLine, VoiceTurn

router = APIRouter(prefix="/api/voice")
# Tickets the browser passes to ElevenLabs as a dynamic variable, which comes back to us as a header on the call's
# WebSocket: they name the chat, so the chat token never goes to ElevenLabs. Single use, and short-lived. In memory,
# like the conversations' locks: the service runs as one instance.
TICKETS: dict[str, tuple[uuid.UUID, float]] = {}
TICKET_SECONDS = 120
# How long the end of a call waits for ElevenLabs to finish its history of it.
HISTORY_WAIT_SECONDS = 60


@router.post("/chat/{token}/session")
async def voice_session(candidate: ChatCandidate, service: Service, request: Request) -> VoiceSession:
    """A call from the chat: a WebRTC token for the browser (our API key stays here), on the engine of the chat's
    current language, and a ticket that names the chat."""
    elevenlabs: AsyncElevenLabs | None = request.app.state.elevenlabs
    language = ScreeningState.model_validate(candidate.state).language
    engine_id = service.settings.speech_engines.get(language)
    if elevenlabs is None or engine_id is None:
        raise HTTPException(status_code=404, detail="Voice calls aren't set up.")
    if candidate.status is Status.WITHDRAWN:
        raise HTTPException(status_code=409, detail="This chat is closed.")
    response = await elevenlabs.conversational_ai.conversations.get_webrtc_token(agent_id=engine_id)
    ticket = secrets.token_urlsafe(24)
    TICKETS[ticket] = (candidate.id, time.monotonic())
    return VoiceSession(conversation_token=response.token, ticket=ticket)


@router.websocket("/ws")
async def speech_engine(websocket: WebSocket) -> None:
    """ElevenLabs connects here once per call. It proves it's ElevenLabs with a JWT signed with our API key, and
    names the chat with the ticket. An edge: errors are logged, and the call's record stays as it was stored."""
    # Not the Service dependency: it reads a Request, which a WebSocket route doesn't have.
    service: ScreeningService = websocket.app.state.service
    elevenlabs: AsyncElevenLabs | None = websocket.app.state.elevenlabs
    api_key = service.settings.elevenlabs_api_key
    ticket = TICKETS.pop(websocket.headers.get("x-voice-ticket", ""), None)
    if elevenlabs is None or api_key is None or ticket is None or time.monotonic() - ticket[1] > TICKET_SECONDS:
        await websocket.close(code=1008)
        return
    try:
        # The SDK's own check (SpeechEngineResource.verify_request calls it), without fetching an engine first:
        # every engine signs with the same key.
        verify_speech_engine_jwt(websocket.headers.get("x-elevenlabs-speech-engine-authorization", ""), api_key)
    except ValueError:
        await websocket.close(code=1008)
        return
    candidate_id = ticket[0]
    await websocket.accept()
    # The SDK wraps a Starlette WebSocket itself (types.wrap_websocket).
    session = SpeechEngineSession(websocket, debug=service.settings.voice_debug)
    call = Call(service, candidate_id, call_id=uuid.uuid4().hex, history_until=await service.last_turn_id(candidate_id))
    session.on(
        "user_transcript",
        lambda transcript: call.answer(transcript, session.send_response, session.conversation_id),
    )
    session.on("error", lambda error: logfire.error("voice call error: {error!r}", error=error))
    with logfire.span("voice call {candidate_id}", candidate_id=candidate_id):
        await session.run()
        try:
            await call.end(session.conversation_id, elevenlabs)
        except Exception:
            logfire.exception("voice call end failed: the last reply is kept as stored")


def words(text: str) -> list[str]:
    """What transcripts are compared by: ElevenLabs may punctuate or case a line differently when it sends it again."""
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def said_words(lines: Sequence[tuple[str, str]]) -> list[str]:
    return [word for role, content in lines if role == "user" for word in words(content)]


def starts(sequence: list[str], prefix: list[str]) -> bool:
    return sequence[: len(prefix)] == prefix


def corrections(record: dict[int, StoredLine], lines: Sequence[tuple[str, str]]) -> list[Correction]:
    """A call's stored lines against a transcript with something new in it from the candidate, or against ElevenLabs'
    history once the call is over: each of our lines now has a line after it, so the transcript says how much of it
    was played (all of it, a part, or nothing: no line of ours at its place). Each of the candidate's lines is as
    transcribed now."""
    found: list[Correction] = []
    for position, line in record.items():
        role, text = lines[position] if position < len(lines) else ("user", "")
        if line.author is Author.ASSISTANT:
            # ElevenLabs marks a line of ours cut before a word of it was played as "...".
            heard = text if role == "agent" and words(text) else ""
            if words(heard) != words(line.text):
                found.append(Correction(message_id=line.message_id, heard=heard or None))
        elif role == "user" and position < len(lines) and text != line.text:
            found.append(Correction(message_id=line.message_id, heard=text))
    return found


@dataclass
class Call:
    """One live call: ElevenLabs' transcripts in, our replies out, and the stored record kept to what was heard.

    A transcript is the whole call as (role, content) lines: "user" for the candidate, as transcribed, and "agent"
    for us, cut to what was played. A line keeps its position as the call goes on, and every line we store carries
    its position in its client id, so the database says what the call has stored (`ScreeningService.call_record`)."""

    service: ScreeningService
    candidate_id: uuid.UUID
    # Ours, for the stored lines' client ids: ElevenLabs' conversation id comes only once the call has started.
    call_id: str
    # The last of our stored turns before the call: the model reads those, then the call from the transcript.
    history_until: int
    # The newest transcript.
    latest: list[tuple[str, str]] = field(default_factory=list)
    # What a turn cut off on the newest transcript had sent to be spoken, and its place in the transcript: no later
    # transcript will show what was heard of it, so the end of the call stores it, then corrects it from ElevenLabs'
    # history.
    cut: tuple[int, str] | None = None

    async def answer(
        self,
        transcript: list[ConversationMessage],
        send_response: Callable[[AsyncIterator[str] | str], Awaitable[None]],
        conversation_id: str | None,
    ) -> None:
        """A transcript from ElevenLabs, ending with what the candidate just said; `send_response` streams our reply
        back (the SDK session's). `conversation_id` is ElevenLabs' id for the call, logged with each turn: the SDK's
        debug log (`VOICE_DEBUG`) names the call by it. ElevenLabs waits for a reply to every transcript, and drops the
        one to the last when it sends a new one: the SDK cancels this then, and the turn with it."""
        received = time.perf_counter()
        sent_lines = [(message.role, message.content) for message in transcript]
        lines = sent_lines
        newest = said_words(self.latest)
        if said_words(lines) != newest and starts(newest, said_words(lines)):
            # An older transcript sent again after a newer one, which our turn on it may have been cancelled by: the
            # newer one is the call as it stands. Seen live: the older one comes back with what it got of the reply
            # that was cut, as a line of ours at its end.
            lines = self.latest
        else:
            self.latest = lines
            # The newest transcript: if a turn was cut off on the one before, this shows what was heard of it.
            self.cut = None
        record = await self.service.call_record(self.candidate_id, self.call_id)
        answered = [
            word for _, line in sorted(record.items()) if line.author is Author.CANDIDATE for word in words(line.text)
        ]
        if starts(answered, said_words(lines)):
            # Nothing the candidate hasn't been answered on: a transcript sent again, sometimes only punctuated anew.
            # A turn on it would read old words against our last question, which the candidate may not have heard
            # yet. Our last reply is said again if the transcript as sent shows none of it played: no line of ours at
            # its place, and the candidate's words last. One that ends with a line of ours asks for nothing.
            last = max((position for position, line in record.items() if line.author is Author.ASSISTANT), default=-1)
            unplayed = record[last].text if last >= len(sent_lines) and sent_lines[-1][0] == "user" else ""
            logfire.info(
                "voice transcript sent again: {action}",
                action="last reply said again" if unplayed else "nothing to say",
                candidate_id=self.candidate_id,
                conversation_id=conversation_id,
            )
            await send_response(unplayed)
            return
        await self.service.correct_heard(self.candidate_id, corrections(record, lines))
        record = await self.service.call_record(self.candidate_id, self.call_id)

        start = len(lines)
        while start > 0 and lines[start - 1][0] == "user":
            start -= 1
        turn = self.service.voice_turn(
            self.candidate_id,
            call_id=self.call_id,
            history_until=self.history_until,
            call=lines[:start],
            said=[content for _, content in lines[start:]],
            new_lines={
                position: line for position, line in enumerate(lines) if position not in record and words(line[1])
            },
        )
        sent: list[str] = []
        stored: VoiceTurn | None = None
        first_text: float | None = None

        async def reply() -> AsyncIterator[str]:
            nonlocal stored, first_text
            async for item in turn:
                if isinstance(item, VoiceTurn):
                    stored = item
                    continue
                first_text = first_text or time.perf_counter()
                sent.append(item)
                yield item

        try:
            # The turn is closed here, in the handler's task, however the reply ends: cut off, it cancels its run at
            # once and stores nothing, so the next transcript's turn doesn't wait for it.
            async with aclosing(turn):
                await send_response(reply())
        finally:
            if stored is None and "".join(sent).strip():
                self.cut = (len(lines), "".join(sent).strip())
            # The epoch time, so a client on the same machine can split its wait into ElevenLabs' share and ours.
            logfire.info(
                "voice turn at {received_at:.3f}: first text {first_text_ms} ms, done {total_ms} ms, "
                "{requests} requests",
                received_at=time.time() - (time.perf_counter() - received),
                first_text_ms=round((first_text - received) * 1000) if first_text else None,
                total_ms=round((time.perf_counter() - received) * 1000),
                requests=stored.requests if stored else None,
                stored=stored is not None,
                candidate_id=self.candidate_id,
                conversation_id=conversation_id,
            )

    async def end(self, conversation_id: str | None, elevenlabs: AsyncElevenLabs) -> None:
        """Once the call is over. First what no turn stored, from the newest transcript, which we hold, rather than
        ElevenLabs' history, which may not be ready: the candidate's words no turn answered, which the text worker
        answers in the chat; what was heard of turns cut off; and the turn cut off at the end, as sent. Then
        everything stored against ElevenLabs' history of the call, which knows how much of each of our lines was
        played, and the call deleted there. We keep what was heard; ElevenLabs keeps nothing."""
        record = await self.service.call_record(self.candidate_id, self.call_id)
        lines = {
            position: line for position, line in enumerate(self.latest) if position not in record and words(line[1])
        }
        if self.cut is not None:
            lines[self.cut[0]] = ("agent", self.cut[1])
        await self.service.end_call(self.candidate_id, self.call_id, lines)
        if conversation_id is None:
            return
        deadline = time.monotonic() + HISTORY_WAIT_SECONDS
        history = await elevenlabs.conversational_ai.conversations.get(conversation_id)
        while history.status not in ("done", "failed") and time.monotonic() < deadline:
            await asyncio.sleep(2)
            history = await elevenlabs.conversational_ai.conversations.get(conversation_id)
        found: list[Correction] = []
        if history.status in ("done", "failed"):
            # A failed call's history is final too: it holds what was played before it failed.
            heard = [(item.role, item.message or "") for item in history.transcript if item.role in ("user", "agent")]
            found = corrections(await self.service.call_record(self.candidate_id, self.call_id), heard)
            await self.service.correct_heard(self.candidate_id, found)
        else:
            logfire.warn("voice call history not ready: our lines kept as stored", conversation_id=conversation_id)
        await elevenlabs.conversational_ai.conversations.delete(conversation_id)
        logfire.info(
            "voice call ended ({status}): {count} corrections from ElevenLabs' history, deleted there",
            status=history.status,
            count=len(found),
            conversation_id=conversation_id,
        )


async def configure_engines(ws_url: str, settings: Settings, config: ClientConfig) -> dict[Language, str]:
    """Create or update one Speech Engine per language in the client config, named after the environment, so the
    local tunnel and the deployment each have their own. Returns their ids."""
    elevenlabs = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
    existing = {
        engine.name: engine.speech_engine_id for engine in (await elevenlabs.speech_engine.list()).speech_engines
    }
    # Biases speech recognition towards what candidates name: places in our areas and delivery platforms.
    keywords = sorted({place for location in config.locations for place in location.places} | set(config.platforms))
    engines: dict[Language, str] = {}
    for language, voice_id in config.voices.items():
        name = f"{SERVICE_NAME}-{settings.environment}-{language}"
        # The browser's ticket comes back on the call's WebSocket as this header.
        speech_engine = SpeechEngineConfig(
            ws_url=ws_url, request_headers={"X-Voice-Ticket": ConvAiDynamicVariable(variable_name="voice_ticket")}
        )
        engine_id = existing.get(name)
        if engine_id is None:
            engine_id = (await elevenlabs.speech_engine.create(name=name, speech_engine=speech_engine)).engine_id
        await elevenlabs.speech_engine.update(
            engine_id,
            speech_engine=speech_engine,
            asr=AsrConversationalConfig(keywords=keywords),
            # ElevenLabs turns numbers and dates into words after we write them. The default, system_prompt, adds
            # instructions to the LLM's prompt instead, and with Speech Engine the LLM is ours.
            tts=TtsConversationalConfigInput(voice_id=voice_id, text_normalisation_type="elevenlabs"),
            language=language.value,
            # No recording, and the shortest retention ElevenLabs takes; Call.end also deletes each call once we've
            # stored what was heard. The process design says audio is deleted after transcription.
            privacy=PrivacyConfigInput(record_voice=False, retention_days=1),
        )
        engines[language] = engine_id
    return engines


if __name__ == "__main__":
    engines = asyncio.run(configure_engines(sys.argv[1], Settings(), load_client_config(DEFAULT_CLIENT_CONFIG)))
    print(f"SPEECH_ENGINES={json.dumps(engines)}")
