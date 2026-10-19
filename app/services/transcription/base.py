from typing import Protocol

from app.core.errors import AppError
from app.schemas.dictation import Language
from app.services.audio import AudioClip


class TranscriptionUpstreamError(AppError):
    status_code = 502
    code = "transcription_upstream_error"


class TranscriberBusyError(AppError):
    status_code = 503
    code = "transcriber_busy"
    headers = {"Retry-After": "5"}


class Transcriber(Protocol):
    name: str

    async def transcribe(self, clip: AudioClip, language: Language) -> str: ...
