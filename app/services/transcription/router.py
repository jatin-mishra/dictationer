from collections.abc import Mapping

from app.schemas.dictation import Language
from app.services.audio import AudioClip
from app.services.transcription.base import Transcriber


class TranscriberRouter:
    def __init__(self, routes: Mapping[Language, Transcriber]) -> None:
        self._routes = dict(routes)

    async def transcribe(self, clip: AudioClip, language: Language) -> tuple[str, str]:
        """Returns (raw text, engine name)."""
        transcriber = self._routes[language]
        return await transcriber.transcribe(clip, language), transcriber.name
