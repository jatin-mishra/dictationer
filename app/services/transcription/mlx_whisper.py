import asyncio
import logging

import numpy as np
import numpy.typing as npt
from starlette.concurrency import run_in_threadpool

from app.schemas.dictation import Language
from app.services.audio import SAMPLE_RATE, AudioClip
from app.services.transcription.base import TranscriberBusyError

log = logging.getLogger(__name__)

# Whisper reads initial_prompt as preceding transcript (style context), not as an instruction.
VERBATIM_PROMPT = "Do not change anything from what is spoken."


class MlxWhisperTranscriber:
    """Local Whisper on Apple Silicon. Metal is effectively one GPU, so inference is serialised
    and callers wait at most `queue_timeout_s` for their turn."""

    name = "mlx-whisper"

    def __init__(self, model_repo: str, queue_timeout_s: float) -> None:
        self._model_repo = model_repo
        self._queue_timeout_s = queue_timeout_s
        self._sem = asyncio.Semaphore(1)

    def _run(self, pcm: npt.NDArray[np.float32]) -> str:
        # Imported lazily: mlx_whisper pulls in mlx + torch, which tests and /health don't need.
        import mlx_whisper

        result = mlx_whisper.transcribe(
            pcm,
            path_or_hf_repo=self._model_repo,
            language="en",
            task="transcribe",
            initial_prompt=VERBATIM_PROMPT,
            condition_on_previous_text=False,  # avoids repetition loops on short clips
        )
        return str(result["text"])

    async def transcribe(self, clip: AudioClip, language: Language) -> str:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._queue_timeout_s)
        except TimeoutError as e:
            raise TranscriberBusyError("Transcriber is busy, please retry") from e
        try:
            return await run_in_threadpool(self._run, clip.pcm)
        finally:
            self._sem.release()

    async def warm_up(self) -> None:
        """Download/load weights once at startup so the first request isn't slow."""
        log.info("warming up %s", self._model_repo)
        await run_in_threadpool(self._run, np.zeros(SAMPLE_RATE, dtype=np.float32))
        log.info("whisper ready")
