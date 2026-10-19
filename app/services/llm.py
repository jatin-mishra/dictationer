import logging
from typing import Literal

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import OutputConfigParam
from pydantic import BaseModel, ValidationError

from app.core.errors import AppError

log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LLMNotConfiguredError(AppError):
    status_code = 502
    code = "llm_not_configured"


class LLMUpstreamError(AppError):
    status_code = 502
    code = "llm_upstream_error"


class LLMRefusedError(AppError):
    status_code = 502
    code = "llm_refused"


class LLMBadOutputError(AppError):
    status_code = 502
    code = "llm_bad_output"


class AgentLLM:
    """Structured-output calls to Claude through the official `anthropic` SDK only.

    The client is built by the caller with the key from os.environ; None means the key or
    model is unset, and every call fails with 502 llm_not_configured (dictation still works).
    """

    def __init__(
        self,
        client: AsyncAnthropic | None,
        model: str,
        effort: Effort | None = None,
        max_tokens: int = 16_000,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    @property
    def configured(self) -> bool:
        return self._client is not None and bool(self._model)

    async def structured[T: BaseModel](
        self, *, system: str, context: str, prompt: str, output: type[T]
    ) -> T:
        """`context` is the large, stable block (outline / book text) and is marked for prompt
        caching; `prompt` is the small part that varies per request and comes last."""
        if self._client is None or not self._model:
            raise LLMNotConfiguredError(
                "ANTHROPIC_AGENT_API_KEY and ANTHROPIC_AGENT_MODEL must be set"
            )
        output_config: OutputConfigParam = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(output)}
        }
        if self._effort is not None:
            output_config["effort"] = self._effort
        try:
            # messages.create, not messages.parse: parse validates the JSON before returning,
            # so a refusal or a truncated reply would surface as a ValidationError and the
            # stop_reason could not be checked.
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": context,
                                "cache_control": {"type": "ephemeral"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                output_config=output_config,
            )
        except anthropic.APIStatusError as e:
            log.warning("llm %s error (request %s): %s", e.status_code, e.request_id, e.message)
            raise LLMUpstreamError(f"LLM returned {e.status_code}") from e
        except anthropic.APIConnectionError as e:  # includes APITimeoutError
            log.warning("llm connection error: %s", e)
            raise LLMUpstreamError("LLM unreachable or timed out") from e

        log.info(
            "llm %s request=%s stop=%s in=%d out=%d cache_read=%s cache_write=%s",
            output.__name__,
            getattr(resp, "_request_id", None),  # set only on real HTTP responses
            resp.stop_reason,
            resp.usage.input_tokens,
            resp.usage.output_tokens,
            resp.usage.cache_read_input_tokens,
            resp.usage.cache_creation_input_tokens,
        )
        if resp.stop_reason == "refusal":
            raise LLMRefusedError("LLM declined the instruction")
        if resp.stop_reason == "max_tokens":
            raise LLMBadOutputError("LLM output was cut off at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMBadOutputError("LLM returned no text")
        try:
            return output.model_validate_json(text)
        except ValidationError as e:
            raise LLMBadOutputError("LLM output does not match the schema") from e
