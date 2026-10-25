from dataclasses import dataclass

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.schemas.dictation import Language
from app.services import audio, script_normalizer, transcript_merge
from app.services.transcription.router import TranscriberRouter


@dataclass(frozen=True)
class Transcript:
    text: str
    engine: str
    warnings: list[str]


async def transcribe_upload(
    upload: UploadFile,
    language: Language,
    transcribers: TranscriberRouter,
    settings: Settings,
    merge_segments: bool = True,
) -> Transcript:
    """Upload → decoded, validated clip → segments → STT → merged, normalised text.

    Shared by dictation and instructions. Raises the audio / transcription AppErrors.
    """
    max_seconds = settings.max_audio_seconds + settings.audio_duration_tolerance_s
    data = await audio.read_upload(upload, settings.max_audio_bytes)
    clip = await run_in_threadpool(audio.decode, data, max_seconds)
    audio.validate(clip, max_seconds, settings.silence_rms_threshold)

    if settings.segment_strategy == "pause":
        cuts = audio.split_at_pauses(
            clip,
            settings.segment_seconds,
            settings.segment_overlap_seconds,
            settings.audio_duration_tolerance_s,
            settings.silence_rms_threshold,
        )
    else:
        fixed = audio.split(
            clip,
            settings.segment_seconds,
            settings.segment_overlap_seconds,
            settings.audio_duration_tolerance_s,
        )
        cuts = [(segment, i > 0) for i, segment in enumerate(fixed)]
    segments = [segment for segment, _ in cuts]
    texts, engine = await transcribers.transcribe_many(
        segments, language, settings.segment_concurrency, settings.silence_rms_threshold
    )
    if not merge_segments:
        # Segment texts appended as-is (overlap repeated) — used to inspect raw overlap.
        text = script_normalizer.guard(" ".join(t.strip() for t in texts if t.strip()))
        return Transcript(text, engine, [])

    # Overlap is removed only where audio was heard twice; texts either side of a pause cut join.
    groups: list[list[str]] = []
    for text, (_, overlaps_previous) in zip(texts, cuts, strict=True):
        if overlaps_previous and groups:
            groups[-1].append(text)
        else:
            groups.append([text])
    merged = (
        transcript_merge.merge(
            group,
            settings.segment_max_overlap_words,
            settings.segment_min_overlap_words,
            settings.segment_max_edge_words,
        )
        for group in groups
    )
    raw = " ".join(t for t in merged if t)
    text, warnings = script_normalizer.normalize(raw, language)
    return Transcript(text, engine, warnings)
