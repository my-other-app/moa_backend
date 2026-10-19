"""Google's Gemini, through its official SDK (google-genai), async.

The platform's provider, and one a club can bring its own key for. The SDK's
own retries are off (one attempt): the runner moves to the next model
instead. Its async client never blocks the event loop.

How Gemini says what went wrong:
- a key that is not valid is a 400 whose reason is API_KEY_INVALID (not a
  401), and a key without access to the API or model is a 403;
- 429 RESOURCE_EXHAUSTED is both "too fast" and "out of quota for today";
- a blocked prompt or a safety stop is an answer with no text and a
  `block_reason` or `finish_reason` saying so.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.core.ai.base import (
    AUTH,
    BAD_REQUEST,
    NOT_FOUND,
    RATE_LIMITED,
    REFUSED,
    SERVER,
    TIMEOUT,
    AIProvider,
    AIRequest,
    AIResult,
    ProviderError,
)

logger = logging.getLogger(__name__)

#: Finish reasons that mean the model stopped rather than answered.
_STOPPED = frozenset({
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "LANGUAGE",
})


def _name(value) -> str:
    return str(getattr(value, "value", value) or "")


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self._client = None

    def _sdk(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
            )
        return self._client

    def _config(self, request: AIRequest, timeout: float):
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=request.system,
            max_output_tokens=request.max_output_tokens,
            http_options=types.HttpOptions(
                timeout=int(timeout * 1000), retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
        level = (settings.AI_GEMINI_THINKING or "").strip().upper()
        if level:
            config.thinking_config = types.ThinkingConfig(thinking_level=level)
        if request.schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = request.schema
        return config

    async def generate(self, request: AIRequest, *, model: str, timeout: float) -> AIResult:
        from google.genai import types

        parts = [types.Part.from_bytes(data=a.data, mime_type=a.mime_type) for a in request.attachments]
        parts.append(types.Part.from_text(text=request.text))
        try:
            response = await asyncio.wait_for(
                self._sdk().aio.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=self._config(request, timeout),
                ),
                # The SDK's own timeout should fire first; this is the backstop.
                timeout=timeout + 5,
            )
        except ProviderError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- classified below, re-raised if not ours
            raise self._classify(exc) from None

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise ProviderError(REFUSED, f"prompt blocked: {_name(feedback.block_reason)}")
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise ProviderError(REFUSED, "no answer")
        reason = _name(getattr(candidates[0], "finish_reason", None))
        if reason in _STOPPED:
            raise ProviderError(REFUSED, f"stopped: {reason}")
        usage = getattr(response, "usage_metadata", None)
        return AIResult(
            text=getattr(response, "text", None) or "",
            model=model,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0)
            + int(getattr(usage, "thoughts_token_count", 0) or 0),
        )

    def _classify(self, exc: BaseException) -> ProviderError:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise exc
        if isinstance(exc, asyncio.TimeoutError):
            return ProviderError(TIMEOUT, "no answer in time")
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            code = int(getattr(exc, "code", 0) or 0)
            status = str(getattr(exc, "status", "") or "")
            message = self.scrub(getattr(exc, "message", "") or "")
            details = self.scrub(str(getattr(exc, "details", "") or ""))
            detail = f"{code} {status} {message}".strip()
            if code in (401, 403) or "API_KEY_INVALID" in details or "API key not valid" in message:
                return ProviderError(AUTH, detail, status=code)
            if code == 429:
                return ProviderError(RATE_LIMITED, detail, status=code)
            if code == 404:
                return ProviderError(NOT_FOUND, detail, status=code)
            if code >= 500:
                return ProviderError(SERVER, detail, status=code)
            return ProviderError(BAD_REQUEST, detail, status=code)
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return ProviderError(TIMEOUT, type(exc).__name__)
        try:
            import httpx2

            if isinstance(exc, (httpx2.TimeoutException, httpx2.TransportError)):
                return ProviderError(TIMEOUT, type(exc).__name__)
        except ImportError:  # pragma: no cover -- httpx2 comes with the anthropic SDK
            pass
        # Anything else is a bug or an SDK surprise: its type, never its
        # message, which could quote the request.
        logger.warning("Gemini call failed with %s", type(exc).__name__)
        return ProviderError(SERVER, type(exc).__name__)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aio.aclose()
                client.close()
            except Exception:  # noqa: BLE001 -- closing is best effort
                pass
