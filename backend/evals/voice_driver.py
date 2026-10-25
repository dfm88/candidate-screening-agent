"""Drive a live voice screening without a microphone, and time it.

A simulated candidate (an LLM playing a persona; synthetic data only) decides what to say; ElevenLabs TTS turns it
into 16 kHz PCM, streamed in real time over the conversation WebSocket the browser would open (WebSocket instead of
WebRTC), followed by silence, as a microphone sends it. Every event is timed on this machine's clock, the clock of a
local API's log, so a turn's wait splits into ElevenLabs' share and ours. Needs the API with voice set up, reachable
by ElevenLabs (a tunnel):

    uv run --group evals --env-file .env python -m evals.voice_driver --persona es

Per turn: time to first audio from the end of the candidate's speech, the longest silence inside the reply (played
back at real time), the model requests our turn made, and what ElevenLabs heard and played. At the end: first audio
on turns that called a tool and on those that didn't, the acknowledgements said before a tool call, and the answers
the database holds, to check against what the candidate said. `--audio DIR` keeps each reply's audio as a WAV file.

`--pause-at N` interrupts our turn N: the candidate stops after the first part of what they say, long enough for
ElevenLabs to send it and our turn to start speaking, then goes on. Time to first audio is then from the end of all of
it; audio that arrives while the candidate is still speaking is reported as talked over.

The split of each wait comes from the API's own records in Logfire, so the run needs LOGFIRE_API_KEY, a read token.
"""

import argparse
import asyncio
import base64
import json
import math
import os
import re
import statistics
import time
import uuid
import wave
from array import array
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import websockets
from elevenlabs import AsyncElevenLabs
from logfire.query_client import LogfireQueryClient
from pydantic_ai import Agent, ModelMessagesTypeAdapter, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.settings import ModelSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from screening.config import Language, Settings
from screening.db import Candidate, make_sessionmaker
from screening.db import Turn as StoredTurn

CHUNK_SECONDS = 0.1
BYTES_PER_SECOND = 32000  # 16 kHz, 16-bit, mono
CANDIDATE_VOICE = "JBFqnCBsd6RMkjVDRZzb"
# vacancy, name, language, persona (written for the simulator, in the persona's language)
PERSONAS = {
    "es": (
        "es-mad-chamberi",
        "Lucía Romero Gil",
        Language.ES,
        "Eres Lucía Romero Gil, 27 años, solicitaste un puesto de repartidora en Madrid. Hablas español de España. "
        "Datos: tienes carné de conducir de coche y moto (B y A2), en vigor; vives en Malasaña, código postal 28004. "
        "Has trabajado un año y medio en Glovo. Puedes trabajar de tarde y fines de semana. Puedes empezar el lunes "
        "que viene. Te parece bien que te tengan en cuenta para otros puestos. En algún momento pregunta cuánto se "
        "cobra.",
    ),
    "es-mx": (
        "mx-cdmx-roma",
        "José Hernández Ruiz",
        Language.ES,
        "Eres José Hernández Ruiz, 34 años, de la Ciudad de México; solicitaste un puesto de repartidor. Hablas "
        "español de México, coloquial. Datos: tienes licencia de manejo tipo A para moto, vigente; vives en la "
        "colonia Roma Sur. Trabajaste dos años en Rappi y unos meses en DiDi Food. Puedes trabajar de lunes a sábado "
        "en las mañanas, tiempo completo. Puedes empezar en dos semanas. Pregunta si necesitas uniforme o mochila "
        "propia.",
    ),
}

simulator = Agent(
    "openai:gpt-6-sol",
    output_type=str,
    model_settings=ModelSettings(temperature=0.7),
    instructions=(
        "You play a job candidate talking on the phone with a recruiting assistant. Reply with only what you say "
        "out loud next: one short spoken sentence or two, natural, no lists, no emojis, in the language of your "
        "persona. Answer only what you were asked; volunteer nothing else. Say numbers the way people say them on the "
        "phone, in words: a postal code digit by digit or in pairs. When the assistant says goodbye or the screening "
        "is over, reply exactly [END]."
    ),
)


def trim_silence(pcm: bytes, threshold: int = 300) -> bytes:
    """TTS pads the clip with silence; the candidate's speech ends at the last loud sample."""
    samples = array("h", pcm)
    loud = [i for i in range(0, len(samples), 160) if max(abs(s) for s in samples[i : i + 160]) > threshold]
    return samples[loud[0] : loud[-1] + 160].tobytes() if loud else pcm


@dataclass
class Turn:
    said: str
    paused: bool = False
    speech_end: float = math.inf
    audio: list[tuple[float, float]] = field(default_factory=list)  # (arrival, seconds)
    # Audio that arrived while the candidate was still speaking: our reply to the first part of a paused line.
    talked_over: float = 0.0
    pcm: bytearray = field(default_factory=bytearray)
    heard: list[str] = field(default_factory=list)
    played: list[str] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)

    def first_audio(self) -> float | None:
        return self.audio[0][0] if self.audio else None

    def played_until(self) -> float:
        """When the reply's audio, played back at real time as it arrives, ends."""
        until = 0.0
        for arrival, seconds in self.audio:
            until = max(until, arrival) + seconds
        return until

    def longest_gap(self) -> float:
        """The longest silence inside the reply, played back at real time as it arrives."""
        gap, played_until = 0.0, 0.0
        for arrival, seconds in self.audio:
            if played_until:
                gap = max(gap, arrival - played_until)
            played_until = max(played_until, arrival) + seconds
        return gap


def played_out(turn: Turn) -> bool:
    """The reply has played out and nothing more came for 2 s, or nothing at all came in 20 s: the candidate speaks."""
    if not turn.audio:
        return time.time() - turn.speech_end > 20
    return time.time() > max(turn.played_until(), turn.audio[-1][0]) + 2


def pause_point(said: str) -> list[str]:
    """Where a candidate stops to think: after the first comma or full stop, else after the first two words."""
    parts = re.split(r"(?<=[,.])\s+", said, maxsplit=1)
    if len(parts) == 2:
        return parts
    words = said.split()
    return [" ".join(words[:2]), " ".join(words[2:])] if len(words) > 2 else [said]


async def speak(elevenlabs: AsyncElevenLabs, text: str, language: Language) -> bytes:
    chunks = elevenlabs.text_to_speech.convert(
        CANDIDATE_VOICE, text=text, model_id="eleven_flash_v2_5", output_format="pcm_16000", language_code=language
    )
    return trim_silence(b"".join([chunk async for chunk in chunks]))


async def run(
    persona: str, api: str, opening: list[str], max_turns: int, pause_at: set[int], pause: float
) -> tuple[str, str, list[Turn]]:
    location, name, language, description = PERSONAS[persona]
    settings = Settings()
    elevenlabs = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
    async with httpx.AsyncClient(base_url=api, timeout=30) as client:
        applied = await client.post(
            "/api/applications",
            json={
                "submission_id": str(uuid.uuid4()),
                "full_name": name,
                "phone": "+34 600 000 000",
                "vacancy_location_id": location,
                "language": language,
                "access_code": settings.apply_access_code,
            },
        )
        candidate_id, token = applied.json()["candidate_id"], applied.json()["chat_url"].rsplit("/", 1)[1]
        ticket = (await client.post(f"/api/voice/chat/{token}/session")).json()["ticket"]
    signed = await elevenlabs.conversational_ai.conversations.get_signed_url(agent_id=settings.speech_engines[language])

    turns: list[Turn] = []
    lines = ["Assistant: (greets you, says there are a few questions about your application, asks to start)"]
    async with websockets.connect(signed.signed_url, max_size=None) as socket:
        start = {"type": "conversation_initiation_client_data", "dynamic_variables": {"voice_ticket": ticket}}
        await socket.send(json.dumps(start))

        async def receive() -> None:
            async for raw in socket:
                message = json.loads(raw)
                kind, now = message["type"], time.time()
                turn = turns[-1] if turns else None
                if kind == "ping":
                    await socket.send(json.dumps({"type": "pong", "event_id": message["ping_event"]["event_id"]}))
                elif turn is None:
                    continue
                elif kind == "audio":
                    pcm = base64.b64decode(message["audio_event"]["audio_base_64"])
                    if now < turn.speech_end:
                        turn.talked_over += len(pcm) / BYTES_PER_SECOND
                        continue
                    turn.audio.append((now, len(pcm) / BYTES_PER_SECOND))
                    turn.pcm += pcm
                elif kind == "user_transcript":
                    turn.heard.append(message["user_transcription_event"]["user_transcript"])
                elif kind == "agent_response":
                    turn.played.append(message["agent_response_event"]["agent_response"])
                elif kind == "agent_response_correction":
                    turn.corrections.append(message["agent_response_correction_event"])

        receiver = asyncio.create_task(receive())
        silence_chunk = base64.b64encode(bytes(int(BYTES_PER_SECOND * CHUNK_SECONDS))).decode()

        async def stream(pcm: bytes) -> float:
            """In real time, as a microphone sends it. Returns when the last chunk went out: the end of speech."""
            size, started = int(BYTES_PER_SECOND * CHUNK_SECONDS), time.time()
            for number, offset in enumerate(range(0, len(pcm), size)):
                await asyncio.sleep(max(0.0, started + (number + 1) * CHUNK_SECONDS - time.time()))
                chunk = base64.b64encode(pcm[offset : offset + size]).decode()
                await socket.send(json.dumps({"user_audio_chunk": chunk}))
            return time.time()

        async def silence(seconds: float, until_played: bool = False) -> None:
            """Silence on the line, as a microphone sends it: for `seconds`, or until the reply has played out."""
            ends = time.time() + seconds
            while time.time() < ends and not (until_played and played_out(turns[-1])):
                await socket.send(json.dumps({"user_audio_chunk": silence_chunk}))
                await asyncio.sleep(CHUNK_SECONDS)

        try:
            await silence(1.0)
            for number in range(max_turns):
                if number < len(opening):
                    said = opening[number]
                else:
                    prompt = f"Persona: {description}\n\nConversation so far:\n" + "\n".join(lines)
                    said = (await simulator.run(prompt)).output.strip()
                if "[END]" in said:
                    break
                parts = pause_point(said) if number + 1 in pause_at else [said]
                clips = [await speak(elevenlabs, part, language) for part in parts]
                turns.append(Turn(said=said, paused=len(clips) > 1))
                for index, clip in enumerate(clips):
                    if index:
                        await silence(pause)
                    speech_end = await stream(clip)
                turns[-1].speech_end = speech_end
                await silence(60, until_played=True)
                turn = turns[-1]
                lines += [f"Candidate: {said}", f"Assistant: {' '.join(turn.played)}"]
                gap = turn.longest_gap()
                first = turn.first_audio()
                ttfa = f"{first - turn.speech_end:.2f}" if first else "-"
                paused = f", paused, {turn.talked_over:.1f} s talked over" if turn.paused else ""
                reply = " ".join(turn.played)
                print(f"turn {number + 1}: first audio {ttfa} s, gap {gap:.2f} s{paused} | {said!r} -> {reply!r}")
        except websockets.ConnectionClosed as closed:
            # A call ElevenLabs ends is a result too: the turns so far are reported.
            print(f"call closed by ElevenLabs: {closed.rcvd.reason if closed.rcvd else closed}")
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
    return candidate_id, token, turns


def server_turns(candidate_id: str) -> list[tuple[float, int | None, int | None]]:
    """Our voice turns for the chat, from the API's Logfire records: (transcript received at, ms to the first text,
    model requests). Needs a Logfire read token (LOGFIRE_API_KEY) for the project the API sends to."""
    query = (
        "select attributes->>'received_at', attributes->>'first_text_ms', attributes->>'requests' from records "
        f"where span_name like 'voice turn at%' and attributes->>'candidate_id' = '{candidate_id}' "
        "order by start_timestamp"
    )
    since = datetime.now(UTC) - timedelta(hours=2)
    with LogfireQueryClient(read_token=os.environ["LOGFIRE_API_KEY"]) as client:
        rows = client.query_json_rows(sql=query, min_timestamp=since)["rows"]
    return [
        (float(received), int(first) if first else None, int(requests) if requests else None)
        for received, first, requests in (tuple(row.values()) for row in rows)
    ]


async def stored(token: str) -> tuple[dict, list[str]] | None:
    """The answers the database holds, and the acknowledgements the candidate heard: the text before a tool call in
    the same response, as stored (cut to what was played, or removed when none of it was). None when the chat isn't
    in the local database: the API was a deployed one."""
    engine = create_async_engine(Settings().database_url)
    async with make_sessionmaker(engine)() as session:
        candidate = await session.scalar(select(Candidate).where(Candidate.chat_token == token))
        turns = (
            []
            if candidate is None
            else await session.scalars(
                select(StoredTurn.messages).where(StoredTurn.candidate_id == candidate.id).order_by(StoredTurn.id)
            )
        )
        responses = [
            message
            for messages in turns
            for message in ModelMessagesTypeAdapter.validate_python(messages)
            if isinstance(message, ModelResponse)
        ]
    await engine.dispose()
    if candidate is None:
        return None
    acknowledgements = [
        " ".join(part.content.strip() for part in response.parts if isinstance(part, TextPart))
        for response in responses
        if any(isinstance(part, ToolCallPart) for part in response.parts)
    ]
    acknowledgements = [text for text in acknowledgements if text]
    answers = {"status": candidate.status, "outcome": candidate.outcome, **candidate.state["answers"]}
    return answers, acknowledgements


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", default="es", choices=PERSONAS)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--opening", action="append", default=[], help="Say these first, in order.")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--out", help="Append one JSON line per turn.")
    parser.add_argument("--audio", help="Write each reply's audio to this directory, as CHAT-TURN.wav.")
    parser.add_argument("--pause-at", type=int, action="append", default=[], help="Interrupt our turn N (from 1).")
    parser.add_argument("--pause", type=float, default=1.6, help="Seconds the candidate stops for. Default: 1.6.")
    arguments = parser.parse_args()

    candidate_id, token, turns = await run(
        arguments.persona,
        arguments.api,
        arguments.opening,
        arguments.max_turns,
        set(arguments.pause_at),
        arguments.pause,
    )
    # Logfire takes a few seconds to make the API's last records queryable.
    await asyncio.sleep(15)
    ours = server_turns(candidate_id)
    print("\nturn  first audio  ElevenLabs in  ours to text  rest  requests  longest gap")
    by_tool: dict[bool, list[float]] = {True: [], False: []}
    after_pause: list[float] = []
    for number, turn in enumerate(turns, 1):
        if arguments.audio and turn.pcm:
            with wave.open(os.path.join(arguments.audio, f"{token}-{number:02}.wav"), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(BYTES_PER_SECOND // 2)
                audio.writeframes(bytes(turn.pcm))
        first = turn.first_audio()
        # The turn that spoke: the last one whose transcript arrived between the end of speech and the first audio.
        spoke = [row for row in ours if first and turn.speech_end - 1 <= row[0] <= first and row[1] is not None]
        if not first or not spoke:
            print(f"{number:>4}  {'-':>11}  (no reply matched)")
            continue
        received, first_text_ms, requests = spoke[-1]
        ours_s = (first_text_ms or 0) / 1000
        if turn.paused:
            after_pause.append(first - turn.speech_end)
        elif requests:
            # A turn that calls a tool makes a second request, for what it says after the tool's result.
            by_tool[requests > 1].append(first - turn.speech_end)
        print(
            f"{number:>4}  {first - turn.speech_end:11.2f}  {received - turn.speech_end:13.2f}  {ours_s:12.2f}  "
            f"{first - received - ours_s:4.2f}  {requests or '-':>8}  {turn.longest_gap():11.2f}"
        )
        if arguments.out:
            with open(arguments.out, "a") as out:  # noqa: ASYNC230 (one line per turn, after the call)
                row = {
                    "persona": arguments.persona,
                    "chat": token,
                    "turn": number,
                    "said": turn.said,
                    "heard": turn.heard,
                    "played": turn.played,
                    "corrections": turn.corrections,
                    "first_audio_s": round(first - turn.speech_end, 2),
                    "elevenlabs_in_s": round(received - turn.speech_end, 2),
                    "ours_to_text_s": ours_s,
                    "requests": requests,
                    "longest_gap_s": round(turn.longest_gap(), 2),
                    "paused": turn.paused,
                    "talked_over_s": round(turn.talked_over, 2),
                }
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
    for label, waits in [("with a tool", by_tool[True]), ("without one", by_tool[False]), ("paused", after_pause)]:
        if waits:
            median, longest = statistics.median(waits), max(waits)
            print(f"first audio, turns {label}: median {median:.2f} s, max {longest:.2f} s, n={len(waits)}")
    found = await stored(token)
    if found is None:
        print("\nstored: not in the local database (a deployed API): see its recruiter tool")
    else:
        answers, acknowledgements = found
        print("\nacknowledgements heard:", json.dumps(acknowledgements, ensure_ascii=False))
        print("stored:", json.dumps(answers, ensure_ascii=False, default=str))
    print("chat:", f"{arguments.api}/chat/{token}")


if __name__ == "__main__":
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    asyncio.run(main())
