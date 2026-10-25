"""WS /v1/dictations/live: stream PCM16 LE 16 kHz mono in, get text back as it is spoken.

Protocol (MVP):
  client: {"type":"start", language, book_name, dictation_type, chapter, page_number,
          author_id?, partials?}, then binary PCM frames, then {"type":"stop"}.
  server: {"type":"ready","engine"}; with partials, {"type":"partial","text","stable",
          "tentative"} while an utterance is open: first after 1 s of it, then per 0.5 s of new
          audio (Whisper) / 1 s (Sarvam). `stable` words never change within the utterance, only
          `tentative` does; text = stable + tentative. Partials replace each other and are never
          stored. {"type":"final","text","warnings"} per utterance; {"type":"done","utterances"};
          on failure {"type":"error","code","message"} and close.
Each final is appended to the book as soon as it is transcribed, so a dropped connection keeps
everything said so far. Finals are processed in order, one at a time. `author-id` comes from the
header, or from start.author_id for browsers (which cannot set WebSocket headers).
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Annotated, Literal

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends, WebSocket, status
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from app.api.dictationer import (
    InvalidAuthorIdError,
    MissingFieldError,
    get_author_id,
    get_chapter_store,
    get_transcriber_router,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.schemas.books import MAX_CHAPTER, MAX_PAGE
from app.schemas.dictation import DictationType, Language
from app.services.audio import SAMPLE_RATE, AudioClip, NoSpeechError
from app.services.streaming import (
    PARTIAL_STEP_DEFAULT,
    PARTIAL_STEP_SAMPLES,
    EnergySpeech,
    PartialCadence,
    SileroSpeech,
    UtteranceSegmenter,
    WordAgreement,
    transcribe_utterance,
)
from app.services.transcription.router import TranscriberRouter
from app.storage.chapter_store import (
    BookNotFoundError,
    ChapterNotFoundError,
    ChapterStore,
    slugify,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["dictation"])


class BadMessageError(AppError):
    status_code = 400
    code = "bad_message"


class BadAudioFrameError(AppError):
    status_code = 400
    code = "bad_audio_frame"


class StartMessage(BaseModel):
    type: Literal["start"]
    language: Language
    # Browsers cannot set WebSocket headers, so author_id may come here instead of `author-id`.
    author_id: str | None = None
    # Re-transcribe the open utterance every second and send it as `partial` (never stored).
    partials: bool = False
    book_name: str = Field(min_length=1, max_length=128)
    # Same rules as POST /v1/dictations: laps sends the lap number in `chapter`.
    dictation_type: DictationType = DictationType.CHAPTER
    chapter: int | None = Field(default=None, ge=1, le=MAX_CHAPTER)
    page_number: int | None = Field(default=None, ge=1, le=MAX_PAGE)


class Target:
    """Where this session's text is appended; checked to exist before any audio is accepted."""

    def __init__(self, store: ChapterStore, author_id: str, start: StartMessage) -> None:
        self.store = store
        self.author_id = author_id
        self.book = slugify(start.book_name)
        self.type = start.dictation_type
        self.chapter = start.chapter
        self.page = start.page_number
        required = {
            DictationType.CHAPTER: {"chapter": self.chapter, "page_number": self.page},
            DictationType.LAPS: {"chapter": self.chapter},
            DictationType.MANUSCRIPT: {},
        }[self.type]
        if missing := [name for name, value in required.items() if value is None]:
            raise MissingFieldError(f"type={self.type.value} requires {', '.join(missing)}")

    async def check_exists(self) -> None:
        if self.type is DictationType.CHAPTER:
            assert self.chapter is not None
            if not await self.store.chapter_exists(self.author_id, self.book, self.chapter):
                raise ChapterNotFoundError(f"Chapter {self.chapter} not found")
        elif not await self.store.book_exists(self.author_id, self.book):
            raise BookNotFoundError(f"Book '{self.book}' not found")

    async def append(self, text: str) -> None:
        if self.type is DictationType.MANUSCRIPT:
            await self.store.append_to_manuscript(self.author_id, self.book, text)
        elif self.type is DictationType.LAPS:
            assert self.chapter is not None
            await self.store.append_to_lap(self.author_id, self.book, self.chapter, text)
        else:
            assert self.chapter is not None and self.page is not None
            await self.store.append_to_page(
                self.author_id, self.book, self.chapter, self.page, text
            )


def decode_frame(data: bytes) -> npt.NDArray[np.float32]:
    if len(data) % 2:
        raise BadAudioFrameError("Binary frames must be PCM16 LE: an even number of bytes")
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def resolve_author_id(header: str | None, from_start: str | None) -> str:
    if header is not None and from_start is not None and header != from_start:
        raise InvalidAuthorIdError("'author-id' header and start.author_id differ")
    return get_author_id(header if header is not None else from_start)


def is_stop(text: str | None) -> bool:
    try:
        return text is not None and json.loads(text) == {"type": "stop"}
    except ValueError:
        return False


async def read_start(websocket: WebSocket) -> StartMessage:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", status.WS_1000_NORMAL_CLOSURE))
    if message.get("text") is None:
        raise BadMessageError("The first message must be a JSON 'start' message")
    try:
        return StartMessage.model_validate_json(message["text"])
    except ValidationError as e:
        raise BadMessageError(f"Invalid 'start' message: {e.errors()[0]['msg']}") from e


@router.websocket("/dictations/live")
async def live_dictation(
    websocket: WebSocket,
    store: Annotated[ChapterStore, Depends(get_chapter_store)],
    transcribers: Annotated[TranscriberRouter, Depends(get_transcriber_router)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """The receive loop only reads frames and queues work; one worker runs every transcription
    (Metal runs one inference at a time anyway), finals first and in order, partials in between.
    """
    await websocket.accept()
    connected = True
    utterances = 0
    threshold = settings.silence_rms_threshold
    detector = SileroSpeech(threshold) if settings.live_vad == "silero" else EnergySpeech(threshold)
    segmenter = UtteranceSegmenter(threshold, detector)
    target: Target | None = None
    language = Language.EN
    partials = False
    cadence = PartialCadence(PARTIAL_STEP_DEFAULT)
    agreement = (-1, WordAgreement())  # (utterance, its agreement)
    finals: deque[tuple[AudioClip, float]] = deque()  # (clip, time it was queued)
    wake = asyncio.Event()
    ending = False  # no more audio: the worker drains `finals` and returns

    async def send(message: dict[str, object]) -> None:
        nonlocal connected
        if not connected:
            return
        try:
            await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError, OSError):
            connected = False  # the client left; committing queued finals goes on

    async def commit(clip: AudioClip, queued: float) -> None:
        nonlocal utterances
        assert target is not None
        started = time.perf_counter()
        try:
            text, warnings = await transcribe_utterance(clip, language, transcribers)
        except NoSpeechError:
            return  # the engine heard nothing in this utterance
        await target.append(text)
        utterances += 1
        await send({"type": "final", "text": text, "warnings": warnings})
        done = time.perf_counter()
        log.info(
            "live final: %.1f s audio, inference %d ms, queued->sent %d ms",
            len(clip.pcm) / SAMPLE_RATE,
            (done - started) * 1000,
            (done - queued) * 1000,
        )

    async def partial(clip: AudioClip, utterance: int) -> None:
        nonlocal agreement
        started = time.perf_counter()
        try:
            text, _ = await transcribe_utterance(clip, language, transcribers)
        except AppError as e:  # best effort: the final for this utterance still comes
            log.debug("partial skipped: %s", e.code)
            return
        # Dropped if its utterance was finalised meanwhile: a partial never follows its final.
        if utterance != segmenter.utterances:
            return
        if agreement[0] != utterance:
            agreement = (utterance, WordAgreement())
        stable, tentative = agreement[1].update(text)
        await send(
            {
                "type": "partial",
                "text": " ".join(t for t in (stable, tentative) if t),
                "stable": stable,
                "tentative": tentative,
            }
        )
        log.debug(
            "live partial: %.1f s audio, inference %d ms",
            len(clip.pcm) / SAMPLE_RATE,
            (time.perf_counter() - started) * 1000,
        )

    def next_partial() -> AudioClip | None:
        if not partials or finals or (clip := segmenter.pending()) is None:
            return None
        return clip if cadence.due(segmenter.utterances, len(clip.pcm)) else None

    async def worker() -> None:
        while True:
            wake.clear()  # cleared before checking: a set() can only happen while we await
            if finals:
                await commit(*finals.popleft())
            elif ending:
                return
            elif (clip := next_partial()) is not None:
                await partial(clip, segmenter.utterances)
            else:
                await wake.wait()

    worker_task: asyncio.Task[None] | None = None
    try:
        start = await read_start(websocket)
        author_id = resolve_author_id(websocket.headers.get("author-id"), start.author_id)
        language, partials = start.language, start.partials
        engine = transcribers.engine_for(start.language)
        cadence = PartialCadence(PARTIAL_STEP_SAMPLES.get(engine, PARTIAL_STEP_DEFAULT))
        target = Target(store, author_id, start)
        await target.check_exists()
        await websocket.send_json({"type": "ready", "engine": engine})
        worker_task = asyncio.create_task(worker())
        while True:
            message = await websocket.receive()
            if worker_task.done():
                worker_task.result()  # a failed commit ends the session with its error
            if message["type"] == "websocket.disconnect":
                connected = False
                break
            if (data := message.get("bytes")) is not None:
                now = time.perf_counter()
                finals.extend((clip, now) for clip in segmenter.feed(decode_frame(data)))
                wake.set()
            elif is_stop(message.get("text")):
                break
            else:
                raise BadMessageError("Expected binary PCM frames or a 'stop' message")
        # Stop or disconnect: the last sentence is often the one still buffered.
        if (tail := segmenter.flush()) is not None:
            finals.append((tail, time.perf_counter()))
        ending = True
        wake.set()
        # Shielded: if this handler is cancelled (shutdown, a test client closing), the worker
        # still finishes the queued finals, which are the user's words.
        await asyncio.shield(worker_task)
        if connected:
            await websocket.send_json({"type": "done", "utterances": utterances})
            await websocket.close()
    except WebSocketDisconnect:
        # The client vanished mid-send; text already committed stays.
        log.info("live dictation disconnected after %d utterances", utterances)
    except AppError as e:
        log.info("live dictation failed: %s %s", e.code, e.message)
        if connected:
            await websocket.send_json({"type": "error", "code": e.code, "message": e.message})
            code = status.WS_1008_POLICY_VIOLATION if e.status_code < 500 else 1011
            await websocket.close(code=code)
    finally:
        if worker_task is not None and not worker_task.done():
            # Finish queued finals (they are the user's words); its inference can't be cancelled.
            ending = True
            wake.set()
            try:
                await asyncio.shield(worker_task)
            except AppError as e:
                log.info("live dictation worker failed: %s", e.code)
