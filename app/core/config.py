from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    max_audio_seconds: float = 10.0
    audio_duration_tolerance_s: float = 0.25
    max_audio_bytes: int = 4 * 1024 * 1024
    silence_rms_threshold: float = 0.003
    idempotency_ttl_s: float = 900.0

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"


@lru_cache
def get_settings() -> Settings:
    """Cached so settings are parsed once; call `get_settings.cache_clear()` in tests."""
    return Settings()
