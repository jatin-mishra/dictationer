import io
import wave
from dataclasses import dataclass

import av
import numpy as np
import numpy.typing as npt
from fastapi import UploadFile

from app.core.errors import AppError

SAMPLE_RATE = 16_000


class AudioTooLargeError(AppError):
    status_code = 413
    code = "audio_too_large"


class UnsupportedAudioError(AppError):
    status_code = 415
    code = "unsupported_audio"


class AudioTooLongError(AppError):
    status_code = 422
    code = "audio_too_long"


class NoSpeechError(AppError):
    status_code = 422
    code = "no_speech"


@dataclass(frozen=True)
class AudioClip:
    """Decoded audio: 16 kHz mono float32 in [-1, 1]."""

    pcm: npt.NDArray[np.float32]

    @property
    def duration(self) -> float:
        return len(self.pcm) / SAMPLE_RATE

    def wav_bytes(self) -> bytes:
        """16-bit PCM WAV — the one format every upstream engine accepts."""
        samples = (np.clip(self.pcm, -1.0, 1.0) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(samples.tobytes())
        return buf.getvalue()


async def read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read at most max_bytes + 1 so an oversized upload is detected without trusting headers."""
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AudioTooLargeError(f"Audio exceeds {max_bytes} bytes")
    if not data:
        raise UnsupportedAudioError("Audio file is empty")
    return data


def decode(data: bytes, max_seconds: float) -> AudioClip:
    """Decode any container/codec ffmpeg knows into 16 kHz mono PCM.

    The format is detected from the bytes, never from filename or Content-Type (mobile clients
    send `application/octet-stream`). Decoding from a seekable BytesIO is required for mobile
    m4a files, whose `moov` atom sits at the end. Decoding stops as soon as the clip exceeds
    max_seconds, so a small but highly compressed file can't make us decode hours of audio.
    """
    max_samples = int(max_seconds * SAMPLE_RATE)
    chunks: list[npt.NDArray[np.float32]] = []
    total = 0
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                raise UnsupportedAudioError("File has no audio stream")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    chunk = out.to_ndarray().reshape(-1).astype(np.float32, copy=False)
                    chunks.append(chunk)
                    total += len(chunk)
                if total > max_samples:
                    raise AudioTooLongError(f"Audio is longer than {max_seconds:g}s")
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1).astype(np.float32, copy=False))
    except av.error.FFmpegError as e:
        raise UnsupportedAudioError("Audio could not be decoded") from e

    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    if pcm.size == 0:
        raise UnsupportedAudioError("Audio contains no samples")
    return AudioClip(pcm)


def is_silent(clip: AudioClip, silence_rms_threshold: float) -> bool:
    rms = float(np.sqrt(np.mean(np.square(clip.pcm, dtype=np.float64))))
    return rms < silence_rms_threshold


def validate(clip: AudioClip, max_seconds: float, silence_rms_threshold: float) -> None:
    if clip.duration > max_seconds:
        raise AudioTooLongError(f"Audio is {clip.duration:.2f}s; max is {max_seconds:g}s")
    if is_silent(clip, silence_rms_threshold):
        raise NoSpeechError("No speech detected in audio")


def split(
    clip: AudioClip, segment_s: float, overlap_s: float, tolerance_s: float
) -> list[AudioClip]:
    """Cut a clip into `segment_s` windows starting every `segment_s - overlap_s` seconds.

    Clips within `segment_s + tolerance_s` are returned whole, so short dictations make exactly
    one engine call as before. The last window is clipped to the end of the audio; windows stop
    as soon as one reaches the end, so no window lies entirely inside the previous one.
    """
    if clip.duration <= segment_s + tolerance_s:
        return [clip]
    size = int(round(segment_s * SAMPLE_RATE))
    stride = size - int(round(overlap_s * SAMPLE_RATE))
    total = len(clip.pcm)
    segments: list[AudioClip] = []
    start = 0
    while True:
        end = min(start + size, total)
        segments.append(AudioClip(clip.pcm[start:end]))  # numpy view, no copy
        if end == total:
            return segments
        start += stride
