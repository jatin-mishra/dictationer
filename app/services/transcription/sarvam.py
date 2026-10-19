import asyncio
import logging
from collections.abc import Sequence

import httpx

from app.schemas.dictation import Language
from app.services.audio import AudioClip
from app.services.transcription.base import TranscriptionUpstreamError

log = logging.getLogger(__name__)

# hi → native script (Devanagari); hinglish → Hindi in Devanagari, English kept in Roman.
_MODES = {Language.HI: "transcribe", Language.HINGLISH: "codemix"}
_LANGUAGE_CODE = "hi-IN"


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class SarvamTranscriber:
    name = "sarvam"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        model: str,
        backoff_s: Sequence[float] = (0.5, 1.0),
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._model = model
        self._backoff_s = backoff_s  # one entry per retry

    async def transcribe(self, clip: AudioClip, language: Language) -> str:
        if not self._api_key:
            raise TranscriptionUpstreamError("Sarvam API key is not configured")
        mode = _MODES[language]
        wav = clip.wav_bytes()

        attempts = len(self._backoff_s) + 1
        for attempt in range(attempts):
            try:
                resp = await self._client.post(
                    "/speech-to-text",
                    headers={"api-subscription-key": self._api_key},
                    data={"model": self._model, "mode": mode, "language_code": _LANGUAGE_CODE},
                    files={"file": ("clip.wav", wav, "audio/wav")},
                )
            except httpx.TransportError as e:  # includes timeouts
                log.warning("sarvam attempt %d/%d failed: %r", attempt + 1, attempts, e)
            else:
                if resp.status_code == 200:
                    return self._transcript(resp)
                log.warning(
                    "sarvam attempt %d/%d: HTTP %d %s",
                    attempt + 1,
                    attempts,
                    resp.status_code,
                    resp.text[:200],
                )
                if not _retryable(resp.status_code):
                    raise TranscriptionUpstreamError(
                        f"Sarvam rejected request ({resp.status_code})"
                    )
            if attempt < len(self._backoff_s):
                await asyncio.sleep(self._backoff_s[attempt])

        raise TranscriptionUpstreamError("Sarvam is unavailable, please retry")

    @staticmethod
    def _transcript(resp: httpx.Response) -> str:
        try:
            transcript = resp.json()["transcript"]
        except (ValueError, KeyError, TypeError) as e:
            raise TranscriptionUpstreamError("Sarvam returned a malformed response") from e
        if not isinstance(transcript, str):
            raise TranscriptionUpstreamError("Sarvam returned a malformed response")
        return transcript
