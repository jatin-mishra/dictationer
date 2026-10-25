import asyncio
from collections.abc import Mapping, Sequence

from app.schemas.dictation import Language
from app.services import audio
from app.services.audio import AudioClip
from app.services.transcription.base import Transcriber


class TranscriberRouter:
    def __init__(self, routes: Mapping[Language, Transcriber]) -> None:
        self._routes = dict(routes)

    def engine_for(self, language: Language) -> str:
        return self._routes[language].name

    async def transcribe(self, clip: AudioClip, language: Language) -> tuple[str, str]:
        """Returns (raw text, engine name)."""
        transcriber = self._routes[language]
        return await transcriber.transcribe(clip, language), transcriber.name

    async def transcribe_many(
        self,
        clips: Sequence[AudioClip],
        language: Language,
        concurrency: int,
        silence_rms_threshold: float,
    ) -> tuple[list[str], str]:
        """Returns (raw text per clip, in clip order; engine name).

        At most `concurrency` engine calls run at once. Silent clips are not sent: they cost an
        STT call and Whisper tends to invent text on silence. Any failure fails the whole call.
        """
        transcriber = self._routes[language]
        sem = asyncio.Semaphore(concurrency)

        async def one(clip: AudioClip) -> str:
            if audio.is_silent(clip, silence_rms_threshold):
                return ""
            async with sem:
                return await transcriber.transcribe(clip, language)

        tasks = [asyncio.ensure_future(one(c)) for c in clips]
        try:
            texts = await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:  # don't keep paying for segments of a request that already failed
                t.cancel()
            raise
        return list(texts), transcriber.name
