"""Voice, through ElevenLabs: live calls, and voice notes in the chat.

Voice notes: a note is transcribed (batch speech-to-text), its transcript deleted at ElevenLabs, and its words go the
text chat's way (`ScreeningService.receive`), marked as a voice note, so the turn reads the voice rules. We keep no
audio. Each of our messages can be played: its speech is made from the stored text each time, and isn't stored.

Live calls, through ElevenLabs Speech Engine. ElevenLabs runs speech-to-text, turn-taking, interruptions and speech,
and for each turn sends the call's transcript to our WebSocket; our turn answers it with the same agent, tools and
state as the text chat (`ScreeningService.voice_turn`), and its reply streams back to be spoken.

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
from typing import Annotated

import logfire
from elevenlabs import (
    AsrConversationalConfig,
    AsyncElevenLabs,
    ConvAiDynamicVariable,
    PrivacyConfigInput,
    SpeechEngineConfig,
    SpeechToTextChunkResponseModel,
    TtsConversationalConfigInput,
)
from elevenlabs.speech_engine import ConversationMessage, SpeechEngineSession, verify_speech_engine_jwt
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import StreamingResponse

from screening.api import ChatCandidate, Service
from screening.config import DEFAULT_CLIENT_CONFIG, SERVICE_NAME, ClientConfig, Language, Settings, load_client_config
from screening.contract import Author, MessageAccepted, MessageKind, VoiceSession
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
# The chat stops recording a note at 60 s: about 0.5 MB in Chrome, which records speech at about 65 kbps. The cap bounds
# what we read and send to be transcribed, with room for a browser that records at a higher rate.
MAX_NOTE_BYTES = 3_000_000
# The model the call engines speak with (ElevenLabs' default for them), so a reply sounds the same in the chat.
SPEECH_MODEL = "eleven_v4_turbo"


def recognition_terms(config: ClientConfig) -> list[str]:
    """What speech recognition is biased towards, in calls and voice notes: what candidates name that a general model
    may not know, the places in our areas and the delivery platforms."""
    return sorted({place for location in config.locations for place in location.places} | set(config.platforms))


@router.post("/chat/{token}/notes", status_code=202)
async def voice_note(
    candidate: ChatCandidate,
    service: Service,
    request: Request,
    client_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> MessageAccepted:
    """A voice note from the chat, in whatever format the browser records: transcribed, and then answered as a typed
    message would be. A note that can't be transcribed, or has no words in it, stores nothing and runs no turn."""
    elevenlabs: AsyncElevenLabs | None = request.app.state.elevenlabs
    if elevenlabs is None:
        raise HTTPException(status_code=404, detail="Voice notes aren't set up.")
    if candidate.status is Status.WITHDRAWN:
        raise HTTPException(status_code=409, detail="This chat is closed.")
    audio = bytearray()
    async for chunk in request.stream():
        audio += chunk
        if len(audio) > MAX_NOTE_BYTES:
            raise HTTPException(status_code=413, detail="This voice note is too long.")
    try:
        transcript = await elevenlabs.speech_to_text.convert(
            model_id="scribe_v2",
            file=("note", bytes(audio), request.headers.get("content-type", "application/octet-stream")),
            # Encoded audio: ElevenLabs reads the container (webm in Chrome, mp4 in Safari).
            file_format="other",
            keyterms=recognition_terms(service.config),
            # A tag like "(laughter)" would be stored as the candidate's words.
            tag_audio_events=False,
            timestamps_granularity="none",
        )
    except Exception:
        logfire.exception("voice note not transcribed", candidate_id=candidate.id, size=len(audio))
        raise HTTPException(status_code=502, detail="The voice note couldn't be transcribed.") from None
    # Another shape comes only with a webhook or with one transcript per channel.
    assert isinstance(transcript, SpeechToTextChunkResponseModel)
    if transcript.transcription_id:
        try:
            # Its transcripts are kept at ElevenLabs until deleted; `enable_logging=False` is for enterprise customers.
            await elevenlabs.speech_to_text.transcripts.delete(transcript.transcription_id)
        except Exception:
            logfire.exception("voice note transcript not deleted at ElevenLabs", candidate_id=candidate.id)
    logfire.info(
        "voice note: {seconds} s, {size} bytes, {language}",
        seconds=transcript.audio_duration_secs,
        size=len(audio),
        language=transcript.language_code,
        candidate_id=candidate.id,
    )
    if not transcript.text.strip():
        raise HTTPException(status_code=422, detail="No words in the voice note.")
    message = await service.receive(candidate, transcript.text.strip(), client_id, MessageKind.VOICE_NOTE)
    return MessageAccepted(id=message.id)


@router.get("/chat/{token}/messages/{message_id}/speech")
async def speech(message_id: int, candidate: ChatCandidate, service: Service, request: Request) -> StreamingResponse:
    """One of our messages, spoken in the voice of the chat's language: made from its stored text each time it's
    played, and streamed as it's made. Nothing is stored on our side; ElevenLabs keeps each one in its history."""
    elevenlabs: AsyncElevenLabs | None = request.app.state.elevenlabs
    message = await service.our_message(candidate.id, message_id)
    if elevenlabs is None or message is None:
        raise HTTPException(status_code=404, detail="No such message.")
    language = ScreeningState.model_validate(candidate.state).language
    audio = elevenlabs.text_to_speech.stream(
        service.config.voices[language],
        text=message.text,
        model_id=SPEECH_MODEL,
        # The language numbers and dates are read in.
        language_code=language.value,
        output_format="mp3_44100_64",
    )
    try:
        # Before the response starts, so a failure is an error status rather than a 200 cut short.
        first = await anext(audio)
    except Exception:
        logfire.exception("speech failed", candidate_id=candidate.id, message_id=message_id)
        raise HTTPException(status_code=502, detail="This message couldn't be spoken.") from None

    async def chunks() -> AsyncIterator[bytes]:
        # Closed here however the response ends, a listener gone included, so the generation stops with it.
        async with aclosing(audio):
            yield first
            async for chunk in audio:
                yield chunk

    return StreamingResponse(chunks(), media_type="audio/mpeg")


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
        # The handler isn't given its transcript's event id, which tells an older transcript sent again from the
        # newest. `_current_event_id` is private, but the SDK (2.74.1, `_handle_message`) cancels the running handler
        # and waits for it before it sets the id and starts the next: a handler that runs reads its own transcript's
        # id. test_voice.py fails if that stops holding.
        lambda transcript: call.answer(
            transcript, session.send_response, session.conversation_id, session._current_event_id
        ),
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
    # The newest transcript, and its event id: ElevenLabs numbers them in order.
    latest: list[tuple[str, str]] = field(default_factory=list)
    latest_event_id: int | None = None
    # What a turn cut off on the newest transcript had sent to be spoken, and its place in the transcript: no later
    # transcript will show what was heard of it, so the end of the call stores it, then corrects it from ElevenLabs'
    # history.
    cut: tuple[int, str] | None = None

    async def answer(
        self,
        transcript: list[ConversationMessage],
        send_response: Callable[[AsyncIterator[str] | str], Awaitable[None]],
        conversation_id: str | None,
        event_id: int | None = None,
    ) -> None:
        """A transcript from ElevenLabs, ending with what the candidate just said; `send_response` streams our reply
        back (the SDK session's). `conversation_id` is ElevenLabs' id for the call, logged with each turn: the SDK's
        debug log (`VOICE_DEBUG`) names the call by it. ElevenLabs waits for a reply to every transcript, and drops the
        one to the last when it sends a new one: the SDK cancels this then, and the turn with it."""
        received = time.perf_counter()
        sent_lines = [(message.role, message.content) for message in transcript]
        lines = sent_lines
        if event_id is not None and self.latest_event_id is not None:
            older = event_id < self.latest_event_id
        else:
            # Without ids, an older transcript is known only when its words begin the newest one's.
            newest = said_words(self.latest)
            older = said_words(lines) != newest and starts(newest, said_words(lines))
        if older:
            # An older transcript sent again after a newer one, which our turn on it may have been cancelled by: the
            # newer one is the call as it stands. Seen live: the older one comes back with what it got of the reply
            # that was cut, as a line of ours at its end; or with an earlier reading of the candidate's words
            # ("Diego Riconi" after "Diego Rigoni"), which the newer one's don't begin with.
            lines = self.latest
        else:
            self.latest = lines
            self.latest_event_id = event_id
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
        played, and the call deleted there. We keep what was heard; ElevenLabs keeps nothing, unless VOICE_RECORDING
        keeps the call there for the owner's test calls. Last, the chat picks up if our last line asks nothing."""
        record = await self.service.call_record(self.candidate_id, self.call_id)
        lines = {
            position: line for position, line in enumerate(self.latest) if position not in record and words(line[1])
        }
        if self.cut is not None:
            lines[self.cut[0]] = ("agent", self.cut[1])
        await self.service.end_call(self.candidate_id, self.call_id, lines)
        try:
            if conversation_id is not None:
                deadline = time.monotonic() + HISTORY_WAIT_SECONDS
                history = await elevenlabs.conversational_ai.conversations.get(conversation_id)
                while history.status not in ("done", "failed") and time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    history = await elevenlabs.conversational_ai.conversations.get(conversation_id)
                found: list[Correction] = []
                if history.status in ("done", "failed"):
                    # A failed call's history is final too: it holds what was played before it failed.
                    heard = [
                        (item.role, item.message or "") for item in history.transcript if item.role in ("user", "agent")
                    ]
                    found = corrections(await self.service.call_record(self.candidate_id, self.call_id), heard)
                    await self.service.correct_heard(self.candidate_id, found)
                else:
                    logfire.warn(
                        "voice call history not ready: our lines kept as stored", conversation_id=conversation_id
                    )
                kept = self.service.settings.voice_recording
                if not kept:
                    await elevenlabs.conversational_ai.conversations.delete(conversation_id)
                logfire.info(
                    "voice call ended ({status}): {count} corrections from ElevenLabs' history, {where}",
                    status=history.status,
                    count=len(found),
                    where="kept there (VOICE_RECORDING)" if kept else "deleted there",
                    conversation_id=conversation_id,
                )
        finally:
            # After the corrections, so our last line is as heard; as stored if ElevenLabs' history failed.
            await self.service.pick_up_after_call(self.candidate_id, self.call_id)


async def configure_engines(ws_url: str, settings: Settings, config: ClientConfig) -> dict[Language, str]:
    """Create or update one Speech Engine per language in the client config, named after the environment, so the
    local tunnel and the deployment each have their own. Returns their ids."""
    elevenlabs = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
    existing = {
        engine.name: engine.speech_engine_id for engine in (await elevenlabs.speech_engine.list()).speech_engines
    }
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
            asr=AsrConversationalConfig(keywords=recognition_terms(config)),
            # ElevenLabs turns numbers and dates into words after we write them. The default, system_prompt, adds
            # instructions to the LLM's prompt instead, and with Speech Engine the LLM is ours.
            tts=TtsConversationalConfigInput(voice_id=voice_id, text_normalisation_type="elevenlabs"),
            language=language.value,
            # No recording, and the shortest retention ElevenLabs takes; Call.end also deletes each call once we've
            # stored what was heard. The process design says audio is deleted after transcription. VOICE_RECORDING
            # (the owner's test calls only) records them; the service sets it again at startup (apply_recording).
            privacy=PrivacyConfigInput(record_voice=settings.voice_recording, retention_days=1),
        )
        engines[language] = engine_id
    return engines


async def apply_recording(elevenlabs: AsyncElevenLabs, settings: Settings) -> None:
    """At startup: the engines record calls as VOICE_RECORDING says, so the Cloud Run console switches it, with no
    deploy and no `python -m screening.voice`. Only that field changes (ElevenLabs merges the update); the rest is
    configure_engines'. An edge: a failure is logged, and that engine keeps its setting."""
    for language, engine_id in settings.speech_engines.items():
        try:
            engine = (await elevenlabs.speech_engine.get(engine_id)).config
            if engine is None or engine.name != f"{SERVICE_NAME}-{settings.environment}-{language}":
                # Another environment's engine (a local .env may name the demo's): its own service sets it.
                logfire.info("voice recording left to its own environment on the {language} engine", language=language)
                continue
            await elevenlabs.speech_engine.update(
                engine_id, privacy=PrivacyConfigInput(record_voice=settings.voice_recording)
            )
        except Exception:
            logfire.exception("voice recording not set on the {language} engine", language=language)
        else:
            logfire.info(
                "voice recording {state} on the {language} engine",
                state="on" if settings.voice_recording else "off",
                language=language,
            )


if __name__ == "__main__":
    engines = asyncio.run(configure_engines(sys.argv[1], Settings(), load_client_config(DEFAULT_CLIENT_CONFIG)))
    print(f"SPEECH_ENGINES={json.dumps(engines)}")
