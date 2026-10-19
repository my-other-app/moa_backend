"""Anthropic's Claude, through its official SDK (anthropic), async.

Only ever with a club's own key: the platform runs on Gemini. The Messages
API with structured output (`output_config.format`) for anything that must
come back as JSON, and `effort: low` -- the answers here are short drafts,
and a long think would not fit the request's time. The SDK's retries are
off (`max_retries=0`): the runner moves to the next model instead.

How Claude says what went wrong:
- 401 is a wrong or revoked key, 403 a key not allowed this model;
- 402 (`billing_error`), or a 400 about the credit balance, is an account
  with nothing left to spend;
- 429 is too many requests, 5xx and 529 the service failing;
- `stop_reason: "refusal"` is a 200 in which the model declined: a failed
  call here, like any other.
"""
from __future__ import annotations

import base64
import logging

from app.core.ai.base import (
    AUTH,
    BAD_REQUEST,
    BILLING,
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

#: The models a club may pick for its own Claude key, best first. Exactly
#: these ids: an alias with a date suffix is never written here.
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")


class ClaudeProvider(AIProvider):
    name = "anthropic"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self._client = None

    def _sdk(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key, max_retries=0)
        return self._client

    @staticmethod
    def _content(request: AIRequest) -> list[dict]:
        blocks: list[dict] = []
        for item in request.attachments:
            data = base64.standard_b64encode(item.data).decode("ascii")
            kind = "document" if item.mime_type == "application/pdf" else "image"
            blocks.append({"type": kind, "source": {"type": "base64", "media_type": item.mime_type, "data": data}})
        blocks.append({"type": "text", "text": request.text})
        return blocks

    async def generate(self, request: AIRequest, *, model: str, timeout: float) -> AIResult:
        output_config: dict = {"effort": "low"}
        if request.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.schema}
        try:
            message = await self._sdk().with_options(timeout=timeout).messages.create(
                model=model,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=[{"role": "user", "content": self._content(request)}],
                output_config=output_config,
            )
        except BaseException as exc:  # noqa: BLE001 -- classified below, re-raised if not ours
            raise self._classify(exc) from None

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise ProviderError(REFUSED, f"refusal: {getattr(details, 'category', None) or 'unspecified'}")
        usage = getattr(message, "usage", None)
        return AIResult(
            text="".join(block.text for block in message.content if getattr(block, "type", None) == "text"),
            model=model,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    def _classify(self, exc: BaseException) -> ProviderError:
        import asyncio

        import anthropic

        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise exc
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderError(TIMEOUT, "no answer in time")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(TIMEOUT, "could not connect")
        if isinstance(exc, anthropic.APIStatusError):
            code = int(getattr(exc, "status_code", 0) or 0)
            kind_name = str(getattr(exc, "type", "") or "")
            message = self.scrub(getattr(exc, "message", "") or "")
            detail = f"{code} {kind_name} {message}".strip()
            if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                return ProviderError(AUTH, detail, status=code)
            if code == 402 or kind_name == "billing_error" or "credit balance" in message.lower():
                return ProviderError(BILLING, detail, status=code)
            if isinstance(exc, anthropic.RateLimitError):
                return ProviderError(RATE_LIMITED, detail, status=code)
            if isinstance(exc, anthropic.NotFoundError):
                return ProviderError(NOT_FOUND, detail, status=code)
            if code >= 500:
                return ProviderError(SERVER, detail, status=code)
            return ProviderError(BAD_REQUEST, detail, status=code)
        logger.warning("Claude call failed with %s", type(exc).__name__)
        return ProviderError(SERVER, type(exc).__name__)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 -- closing is best effort
                pass
