from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration. Values come from the environment, then `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "dictationer"
    environment: Literal["local", "dev", "staging", "prod"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    cors_origins: list[str] = []

    # Sarvam (hi / hinglish). The API key is a secret: read from os.environ, never from here.
    sarvam_model: str = "saaras:v4"
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_timeout_s: float = 8.0

    # Local MLX Whisper (en)
    whisper_model_repo: str = "mlx-community/whisper-large-v3-turbo"
    whisper_preload: bool = True
    whisper_queue_timeout_s: float = 20.0

    # Dictation input and storage
    data_root: Path = Path("data")
    max_audio_seconds: float = 60.0
    audio_duration_tolerance_s: float = 0.25
    max_audio_bytes: int = 12 * 1024 * 1024
    silence_rms_threshold: float = 0.003
    idempotency_ttl_s: float = 900.0

    # Long audio is split into overlapping segments, transcribed in parallel, then merged.
    segment_seconds: float = 10.0
    segment_overlap_seconds: float = 2.0
    segment_max_overlap_words: int = 12
    segment_min_overlap_words: int = 2
    segment_max_edge_words: int = 3  # garbled words tolerated at each side of a cut
    segment_concurrency: int = 3
    segment_merge: bool = True  # false: append segment texts as-is (for testing overlap)

    # Instruction LLM (Anthropic). Key and model are read from os.environ
    # (ANTHROPIC_AGENT_API_KEY / ANTHROPIC_AGENT_MODEL), never from here.
    llm_timeout_s: float = 60.0
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None  # sent only if set
    instruction_max_book_chars: int = 300_000  # whole-book search budget (no chapter spoken)

    @model_validator(mode="after")
    def _check_segmentation(self) -> Self:
        if self.segment_seconds <= 0:
            raise ValueError("segment_seconds must be > 0")
        if not 0 <= self.segment_overlap_seconds < self.segment_seconds:
            raise ValueError("segment_overlap_seconds must be >= 0 and < segment_seconds")
        if self.segment_concurrency < 1:
            raise ValueError("segment_concurrency must be >= 1")
        if not 1 <= self.segment_min_overlap_words <= self.segment_max_overlap_words:
            raise ValueError("need 1 <= segment_min_overlap_words <= segment_max_overlap_words")
        if self.segment_max_edge_words < 0:
            raise ValueError("segment_max_edge_words must be >= 0")
        return self

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"


@lru_cache
def get_settings() -> Settings:
    """Cached so settings are parsed once; call `get_settings.cache_clear()` in tests."""
    return Settings()
