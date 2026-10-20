"""Which provider, with which key, through which models.

- `PROVIDERS`: every provider there is a class for. A third (OpenAI, say)
  is a class in its own module and one line here.
- `make`: a provider for a key. Tests swap `_factory` for a fake, so nothing
  in the suite reaches the network.
- `platform_routes`: what the platform's own key goes through: Gemini with
  APP_GEMINI_API_KEY over APP_AI_MODELS (APP_AI_VISION_MODELS for a poster
  or a PDF), then -- only if APP_AI_FALLBACK_PROVIDER names one, with its own
  APP_AI_FALLBACK_API_KEY -- that provider's models. Unset by default.
- `club_route`: a club's own key: its provider, the model it picked, then
  the lighter models listed after that one (never a dearer one: the club
  pays for every call).

One key per provider, always. Nothing here rotates between keys or
accounts to get past a provider's rate limits; the fallback is between
models (each has its own limits), and the next provider is a different
service, not a second account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config import settings
from app.core.ai import claude
from app.core.ai.base import AIProvider
from app.core.ai.claude import ClaudeProvider
from app.core.ai.gemini import GeminiProvider

GEMINI = GeminiProvider.name
ANTHROPIC = ClaudeProvider.name

PROVIDERS: dict[str, type[AIProvider]] = {
    GEMINI: GeminiProvider,
    ANTHROPIC: ClaudeProvider,
}

#: Swapped by the tests: (provider name, api key) -> AIProvider.
_factory: Optional[Callable[[str, str], AIProvider]] = None


def make(name: str, api_key: str) -> AIProvider:
    if _factory is not None:
        return _factory(name, api_key)
    return PROVIDERS[name](api_key)


def model_list(raw: str) -> list[str]:
    """A comma-separated setting as a list, blanks and repeats dropped, in order."""
    out: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def gemini_models(*, vision: bool = False) -> list[str]:
    return model_list(settings.AI_VISION_MODELS if vision else settings.AI_MODELS)


def selectable() -> dict[str, list[str]]:
    """The models a club may pick for its own key, per provider, best first."""
    gemini = gemini_models()
    for model in gemini_models(vision=True):
        if model not in gemini:
            gemini.append(model)
    return {GEMINI: gemini, ANTHROPIC: list(claude.MODELS)}


@dataclass(frozen=True)
class Route:
    """One provider and key, and the models to try with it, in order."""

    provider: str
    api_key: str = field(repr=False)
    models: tuple[str, ...]
    byok: bool = False


def platform_routes(*, vision: bool) -> list[Route]:
    """The platform's routes. Empty when no key is set: nothing can be asked."""
    routes: list[Route] = []
    key = (settings.GEMINI_API_KEY or "").strip()
    models = gemini_models(vision=vision)
    if key and models:
        routes.append(Route(provider=GEMINI, api_key=key, models=tuple(models)))
    name = (settings.AI_FALLBACK_PROVIDER or "").strip().lower()
    fallback_key = (settings.AI_FALLBACK_API_KEY or "").strip()
    if name in PROVIDERS and fallback_key:
        fallback = model_list(settings.AI_FALLBACK_MODELS) or selectable()[name]
        routes.append(Route(provider=name, api_key=fallback_key, models=tuple(fallback)))
    return routes


def club_route(provider: str, api_key: str, model: Optional[str]) -> Route:
    """A club's own key: the model it picked first, then the lighter ones
    listed after it for that provider."""
    listed = selectable().get(provider, [])
    if model in listed:
        chain = listed[listed.index(model):]
    elif model:
        chain = [model]
    else:
        chain = listed[:1]
    return Route(provider=provider, api_key=api_key, models=tuple(chain), byok=True)
