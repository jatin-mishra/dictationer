"""Live dictation: cut a never-ending PCM stream into utterances at pauses.

No engine streams, so each utterance is transcribed as a complete clip. Cutting at a pause means no
word is split, so utterance texts are simply appended: there is no overlap to merge.

A pause is "no speech", which loudness alone can't tell: a silent room through a real microphone is
louder than `silence_rms_threshold`, and Whisper invents text for that noise ("Thank you.", its own
prompt). SileroSpeech (a small speech/non-speech model) is what live dictation uses;
EnergySpeech (loudness only) remains for synthetic audio such as test tones.
"""

import math
from typing import Protocol

import numpy as np
import numpy.typing as npt

from app.schemas.dictation import Language
from app.services import script_normalizer
from app.services.audio import FRAME, SAMPLE_RATE, AudioClip
from app.services.transcription.router import TranscriberRouter

END_SILENCE_MS = 600  # this much non-speech ends an utterance
PREROLL_MS = 300  # kept before speech starts, so soft first consonants are not clipped
MIN_VOICED_MS = 250  # shorter blips are clicks, and Whisper invents text for them
MAX_UTTERANCE_SAMPLES = 20 * SAMPLE_RATE  # run-on speech is cut at 20 s regardless
PARTIAL_FIRST_SAMPLES = SAMPLE_RATE  # first partial once an utterance holds 1 s of audio
# Then one per this much new audio. Whisper (local, ~0.7 s per pass) runs back to back; Sarvam is
# a paid API, so at most once per second.
PARTIAL_STEP_SAMPLES = {"mlx-whisper": SAMPLE_RATE // 2}
PARTIAL_STEP_DEFAULT = SAMPLE_RATE
SILERO_FRAME = 512  # 32 ms: the window Silero VAD scores at 16 kHz
SILERO_SPEECH_PROB = 0.5


def _rms(frame: npt.NDArray[np.float32]) -> float:
    return float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))


class SpeechDetector(Protocol):
    """Says whether one frame of `frame` samples is speech. May keep state across frames."""

    frame: int

    def __call__(self, frame: npt.NDArray[np.float32]) -> bool: ...


class EnergySpeech:
    """Loudness only: any sound above the threshold counts as speech."""

    frame = FRAME

    def __init__(self, silence_rms_threshold: float) -> None:
        self._threshold = silence_rms_threshold

    def __call__(self, frame: npt.NDArray[np.float32]) -> bool:
        return _rms(frame) >= self._threshold


class SileroSpeech:
    """Silero VAD (bundled with faster-whisper, run on CPU via onnxruntime, ~0.2 ms per frame):
    speech must be both audible and scored as speech, so room noise and hum never open an utterance.
    """

    frame = SILERO_FRAME

    def __init__(self, silence_rms_threshold: float) -> None:
        from faster_whisper.vad import get_vad_model  # lazy: loads onnxruntime

        self._model = get_vad_model()  # cached: one model shared by all sessions
        self._threshold = silence_rms_threshold
        self._previous = np.zeros(SILERO_FRAME, dtype=np.float32)  # gives the model its context

    def __call__(self, frame: npt.NDArray[np.float32]) -> bool:
        window = np.concatenate([self._previous, frame])
        self._previous = frame
        if _rms(frame) < self._threshold:
            return False  # digital silence: no need to ask the model
        return float(np.asarray(self._model(window)).reshape(-1)[-1]) >= SILERO_SPEECH_PROB


class UtteranceSegmenter:
    """Feed PCM in any chunk size; get back each utterance once it is followed by a pause.

    Synchronous; output depends only on the samples, not on how they were chunked.
    """

    def __init__(
        self, silence_rms_threshold: float, detector: SpeechDetector | None = None
    ) -> None:
        self._is_speech = detector or EnergySpeech(silence_rms_threshold)
        self._frame = self._is_speech.frame
        self._end_silence = math.ceil(END_SILENCE_MS * SAMPLE_RATE / 1000 / self._frame)
        self._preroll_frames = math.ceil(PREROLL_MS * SAMPLE_RATE / 1000 / self._frame)
        self._min_voiced = math.ceil(MIN_VOICED_MS * SAMPLE_RATE / 1000 / self._frame)
        self._rest = np.zeros(0, dtype=np.float32)  # < 1 frame carried to the next feed
        self._preroll: list[npt.NDArray[np.float32]] = []
        self._frames: list[npt.NDArray[np.float32]] = []  # the open utterance
        self._samples = 0
        self._voiced = 0
        self._quiet = 0
        self._speaking = False
        self.utterances = 0  # ended utterances (kept or dropped): identifies the open one

    def feed(self, pcm: npt.NDArray[np.float32]) -> list[AudioClip]:
        buf = np.concatenate([self._rest, pcm])
        size = self._frame
        n = len(buf) // size
        self._rest = buf[n * size :]
        clips: list[AudioClip] = []
        for i in range(n):
            frame = buf[i * size : (i + 1) * size]
            voiced = self._is_speech(frame)
            if not self._speaking:
                if voiced:
                    self._speaking = True
                    self._frames, self._preroll = [*self._preroll, frame], []
                    self._samples = sum(len(f) for f in self._frames)
                    self._voiced, self._quiet = 1, 0
                else:
                    self._preroll = [*self._preroll, frame][-self._preroll_frames :]
                continue
            self._frames.append(frame)
            self._samples += size
            if voiced:
                self._voiced, self._quiet = self._voiced + 1, 0
            else:
                self._quiet += 1
            if self._quiet >= self._end_silence:
                self._speaking = False
                clips += self._emit()
            elif self._samples >= MAX_UTTERANCE_SAMPLES:
                clips += self._emit()  # still speaking: the next frame continues a new utterance
        return clips

    def flush(self) -> AudioClip | None:
        """The open utterance (plus any partial frame), e.g. when the client stops mid-sentence."""
        if not self._speaking:
            return None
        self._frames.append(self._rest)
        self._rest = np.zeros(0, dtype=np.float32)
        self._speaking = False
        clips = self._emit()
        return clips[0] if clips else None

    def pending(self) -> AudioClip | None:
        """The open utterance so far, for partial transcripts: only while speech is ongoing (not in
        the trailing silence, when the final is moments away) and once it holds real speech."""
        if not self._speaking or self._quiet or self._voiced < self._min_voiced:
            return None
        return AudioClip(np.concatenate(self._frames))

    def _emit(self) -> list[AudioClip]:
        self.utterances += 1
        pcm = np.concatenate(self._frames)
        keep = self._voiced >= self._min_voiced
        self._frames, self._samples, self._voiced, self._quiet = [], 0, 0, 0
        return [AudioClip(pcm)] if keep else []


class PartialCadence:
    """When the next partial is due: the first once an utterance holds `PARTIAL_FIRST_SAMPLES`,
    then one per `step` samples of new audio. Tracks one utterance at a time."""

    def __init__(self, step: int) -> None:
        self._step = step
        self._utterance = -1
        self._done = 0  # samples covered by the last partial of `_utterance`

    def due(self, utterance: int, samples: int) -> bool:
        """True (and recorded) if a partial of `samples` should start now."""
        done = self._done if utterance == self._utterance else 0
        if samples - done < (self._step if done else PARTIAL_FIRST_SAMPLES):
            return False
        self._utterance, self._done = utterance, samples
        return True


def _word_key(word: str) -> str:
    return "".join(c for c in word.lower() if c.isalnum())


class WordAgreement:
    """Steady partial text (LocalAgreement-2, as in whisper_streaming): a word is locked once two
    passes in a row agree on it. Locked words keep the spelling first shown and never change,
    so only the tentative tail moves. One instance per utterance."""

    def __init__(self) -> None:
        self._stable: list[str] = []
        self._previous: list[str] = []

    def update(self, hypothesis: str) -> tuple[str, str]:
        """Returns (stable, tentative) text for this pass."""
        words = hypothesis.split()
        n = len(self._stable)
        for old, new in zip(self._previous[n:], words[n:], strict=False):
            if _word_key(old) != _word_key(new):
                break
            self._stable.append(old)
        self._previous = words
        return " ".join(self._stable), " ".join(words[len(self._stable) :])


async def transcribe_utterance(
    clip: AudioClip, language: Language, transcribers: TranscriberRouter
) -> tuple[str, list[str]]:
    """(normalised text, script warnings). Raises NoSpeechError when the engine hears nothing."""
    raw, _ = await transcribers.transcribe(clip, language)
    return script_normalizer.normalize(raw, language)
