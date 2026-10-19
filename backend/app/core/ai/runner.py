"""Asking: model after model, until one gives an answer that parses.

`complete(routes, request, parse)` tries each route's models in order. A
model that is rate limited, failing, too slow, unknown, or that declines is
followed by the next, after a short pause (APP_AI_FALLBACK_BACKOFF_SECONDS).
A refused key or an empty account ends that route at once: every model
behind it would say the same.

The answer must parse (`parse` raises ValueError, or pydantic's
ValidationError, otherwise): a model that answers with something else is
asked once more, the same way, and if it does it again the request fails
(`invalid_output`) -- a model that cannot follow the schema is not given to
the next one to guess at.

Each call gets APP_AI_TIMEOUT_SECONDS (APP_AI_VISION_TIMEOUT_SECONDS with a
file), and the whole of it at most APP_AI_DEADLINE_SECONDS, which stays
under nginx's two minutes. Nothing here logs a prompt or an answer: only
the provider, the model, what went wrong and the provider's own words for
it, with the key taken out.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from app.config import settings
from app.core.ai import providers
from app.core.ai.base import (
    AUTH,
    BILLING,
    NEXT_MODEL,
    RATE_LIMITED,
    TIMEOUT,
    AIRequest,
    ProviderError,
)

logger = logging.getLogger(__name__)

#: Kinds of failure the runner adds to the providers' own.
INVALID_OUTPUT = "invalid_output"
NOT_CONFIGURED = "not_configured"

RETRY_NOTE = (
    "\n\nYour previous answer did not match the required JSON schema. "
    "Answer again with only the JSON object, exactly as the schema says."
)


@dataclass
class Outcome:
    value: Any
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class AIFailure(Exception):
    """No route gave a usable answer. `kind` decides what the caller is told."""

    def __init__(self, kind: str, *, provider: str = "", model: str = "", input_tokens: int = 0,
                 output_tokens: int = 0, latency_ms: int = 0, byok: bool = False):
        self.kind = kind
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.byok = byok
        super().__init__(kind)


def parse_json(text: str) -> Any:
    """The JSON in a model's answer: the whole answer, or what is inside a
    ``` fence when a model wraps it in one. ValueError when there is none."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    if not body:
        raise ValueError("empty answer")
    return json.loads(body)


def _final_kind(kinds: list[str]) -> str:
    if not kinds:
        return NOT_CONFIGURED
    for kind in (AUTH, BILLING):
        if kind in kinds:
            return kind
    if all(kind == RATE_LIMITED for kind in kinds):
        return RATE_LIMITED
    return kinds[-1]


async def _pause(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


async def complete(
    routes: list[providers.Route],
    request: AIRequest,
    parse: Callable[[str], Any],
    *,
    sleep: Optional[Callable[[float], Any]] = None,
) -> Outcome:
    """The first answer that parses, from the first route and model that gives
    one. Raises AIFailure."""
    sleep = sleep or _pause
    started = time.monotonic()
    deadline = started + float(settings.AI_DEADLINE_SECONDS)
    per_call = float(settings.AI_VISION_TIMEOUT_SECONDS if request.vision else settings.AI_TIMEOUT_SECONDS)
    backoff = float(settings.AI_FALLBACK_BACKOFF_SECONDS)
    byok = any(route.byok for route in routes)
    kinds: list[str] = []
    tokens_in = tokens_out = 0
    last_provider = last_model = ""
    first = True

    def failure(kind: str) -> AIFailure:
        return AIFailure(
            kind, provider=last_provider, model=last_model, input_tokens=tokens_in, output_tokens=tokens_out,
            latency_ms=int((time.monotonic() - started) * 1000), byok=byok,
        )

    def time_left() -> float:
        return deadline - time.monotonic()

    for route in routes:
        provider = providers.make(route.provider, route.api_key)
        try:
            for model in route.models:
                if not first:
                    await sleep(backoff)
                first = False
                if time_left() < 1:
                    kinds.append(TIMEOUT)
                    raise failure(_final_kind(kinds))
                last_provider, last_model = route.provider, model
                try:
                    result = await provider.generate(request, model=model, timeout=min(per_call, time_left()))
                except ProviderError as exc:
                    kinds.append(exc.kind)
                    logger.warning("AI %s %s failed: %s %s", route.provider, model, exc.kind, provider.scrub(exc.detail))
                    if exc.kind in NEXT_MODEL:
                        continue
                    break  # this key: the rest of its models would say the same
                tokens_in += result.input_tokens
                tokens_out += result.output_tokens
                try:
                    value = parse(result.text)
                except ValueError:  # pydantic's ValidationError is one
                    logger.warning("AI %s %s answered off the schema; asking once more", route.provider, model)
                    if time_left() < 1:
                        raise failure(INVALID_OUTPUT)
                    try:
                        result = await provider.generate(
                            replace(request, text=request.text + RETRY_NOTE), model=model, timeout=min(per_call, time_left())
                        )
                    except ProviderError as exc:
                        kinds.append(exc.kind)
                        logger.warning("AI %s %s failed: %s %s", route.provider, model, exc.kind, provider.scrub(exc.detail))
                        if exc.kind in NEXT_MODEL:
                            continue
                        break
                    tokens_in += result.input_tokens
                    tokens_out += result.output_tokens
                    try:
                        value = parse(result.text)
                    except ValueError:
                        logger.warning("AI %s %s answered off the schema twice", route.provider, model)
                        raise failure(INVALID_OUTPUT)
                return Outcome(
                    value=value, provider=route.provider, model=model, input_tokens=tokens_in, output_tokens=tokens_out,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
        finally:
            await provider.aclose()
    raise failure(_final_kind(kinds))
