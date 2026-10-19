"""What every AI provider is to the rest of the code: one class, one call.

A provider takes an `AIRequest` (a system instruction, the text, any files,
and the JSON schema the answer must follow) and one model's id, and returns
the answer's text with what it cost in tokens. Everything a provider can go
wrong with is a `ProviderError` of one of a few kinds, so the runner
(runner.py) can decide what to do next without knowing whose SDK said it:

- `auth`: the key was refused (wrong, revoked, or not allowed this model).
- `billing`: the key's account has no credit or quota left.
- `rate_limited`: too many requests, or the quota for now (HTTP 429).
- `not_found`: the model id is unknown to the provider.
- `server`: the provider failed (5xx, overloaded).
- `timeout`: no answer in time, or the connection failed.
- `refused`: the model declined (a safety stop, a blocked prompt).
- `bad_request`: the provider refused the request itself (a 400 that is not
  about the key).

Adding a provider (OpenAI, say) is one subclass in a module of its own and
one line in providers.PROVIDERS.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

AUTH = "auth"
BILLING = "billing"
RATE_LIMITED = "rate_limited"
NOT_FOUND = "not_found"
SERVER = "server"
TIMEOUT = "timeout"
REFUSED = "refused"
BAD_REQUEST = "bad_request"
KINDS = (AUTH, BILLING, RATE_LIMITED, NOT_FOUND, SERVER, TIMEOUT, REFUSED, BAD_REQUEST)

#: The kinds after which the next model in the list is tried. A refused key
#: or an empty account is the same for every model, so those stop the list.
NEXT_MODEL = frozenset({RATE_LIMITED, NOT_FOUND, SERVER, TIMEOUT, REFUSED, BAD_REQUEST})


class ProviderError(Exception):
    """A provider call that did not give an answer. `detail` is safe to log:
    the provider's own words with the key taken out (see `scrub`)."""

    def __init__(self, kind: str, detail: str = "", *, status: int | None = None):
        self.kind = kind if kind in KINDS else SERVER
        self.detail = (detail or "")[:300]
        self.status = status
        super().__init__(f"{self.kind}: {self.detail}" if self.detail else self.kind)


@dataclass(frozen=True)
class Attachment:
    """A file sent with the request: a poster (PNG, JPEG, WebP) or a PDF."""

    mime_type: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class AIRequest:
    """One question to a model.

    `system` is ours (instructions); `text` carries what the club wrote,
    already wrapped in data blocks (prompting.data_block). `schema` is the
    JSON Schema the answer must follow; with none, the answer is plain text.
    """

    system: str
    text: str
    attachments: tuple[Attachment, ...] = ()
    schema: dict | None = None
    max_output_tokens: int = 8192

    @property
    def vision(self) -> bool:
        return bool(self.attachments)


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class AIProvider(ABC):
    """One provider's SDK behind the one call the runner makes.

    Made per request with the key it is to use (the platform's, or a club's
    own) and closed after (`aclose`), so no client outlives the request or
    is shared between keys."""

    #: The name stored with a club's key and in ai_usage.provider.
    name: str = ""

    def __init__(self, api_key: str):
        self._api_key = api_key

    @abstractmethod
    async def generate(self, request: AIRequest, *, model: str, timeout: float) -> AIResult:
        """The model's answer, or a ProviderError. Never retries: the runner
        decides whether another model is tried."""

    async def check_key(self, *, model: str, timeout: float) -> None:
        """One tiny call to see that the key works with `model`. Raises
        ProviderError like `generate`."""
        await self.generate(
            AIRequest(system="Reply with the single word OK.", text="OK?", max_output_tokens=64),
            model=model,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        """Let go of the HTTP client. Safe to call twice."""

    def scrub(self, text: str) -> str:
        """`text` with this provider's key taken out, wherever it appears."""
        text = str(text or "")
        if self._api_key and len(self._api_key) >= 6:
            text = text.replace(self._api_key, "[Filtered]")
        return text
