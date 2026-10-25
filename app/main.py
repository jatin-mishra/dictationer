import logging
import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import ollama
from anthropic import AsyncAnthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.middleware.request_id import RequestIdMiddleware
from app.schemas.dictation import Language
from app.services.idempotency import IdempotencyCache
from app.services.llm import AgentLLM
from app.services.locator import Locator
from app.services.transcription.mlx_whisper import MlxWhisperTranscriber
from app.services.transcription.router import TranscriberRouter
from app.services.transcription.sarvam import SarvamTranscriber
from app.storage.chapter_store import ChapterStore

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("starting %s (%s)", settings.app_name, settings.environment)

    async with AsyncExitStack() as stack:
        sarvam_http = await stack.enter_async_context(
            httpx.AsyncClient(base_url=settings.sarvam_base_url, timeout=settings.sarvam_timeout_s)
        )
        sarvam_api_key = os.environ.get("SARVAM_API_KEY", "")
        sarvam = SarvamTranscriber(sarvam_http, sarvam_api_key, settings.sarvam_model)
        whisper = MlxWhisperTranscriber(
            settings.whisper_model_repo, settings.whisper_queue_timeout_s
        )
        app.state.transcriber_router = TranscriberRouter(
            {Language.HI: sarvam, Language.HINGLISH: sarvam, Language.EN: whisper}
        )
        app.state.chapter_store = ChapterStore(settings.data_root)
        app.state.idempotency_cache = IdempotencyCache(settings.idempotency_ttl_s)

        # Secrets from os.environ only. api_key is passed explicitly so the SDK never falls back
        # to ANTHROPIC_API_KEY or a local login profile.
        llm_api_key = os.environ.get("ANTHROPIC_AGENT_API_KEY", "")
        llm_model = os.environ.get("ANTHROPIC_AGENT_MODEL", "")
        llm_client = None
        if llm_api_key and llm_model:
            llm_client = await stack.enter_async_context(
                AsyncAnthropic(api_key=llm_api_key, timeout=settings.llm_timeout_s, max_retries=2)
            )
        else:
            log.warning(
                "ANTHROPIC_AGENT_API_KEY / ANTHROPIC_AGENT_MODEL not set: /instructions will fail"
            )
        app.state.agent_llm = AgentLLM(llm_client, llm_model, settings.llm_effort)
        ollama_client = await stack.enter_async_context(
            ollama.AsyncClient(host=settings.ollama_host, timeout=settings.locator_timeout_s)
        )
        app.state.locator = Locator(ollama_client, settings.locator_model)

        if not sarvam_api_key:
            log.warning("SARVAM_API_KEY is not set: hi / hinglish dictation will fail")
        if settings.whisper_preload:
            await whisper.warm_up()
        yield
    log.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
