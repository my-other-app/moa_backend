"""A club's own AI key: connected, tested, encrypted, and taken away.

The club's owner or an admin member pastes a Gemini or Anthropic key and
picks a model. One tiny call with it, before anything is saved: a key that
fails it is refused (422 AI_KEY_INVALID, saying why) and never stored. A
good one is encrypted (app/core/ai/crypto.py) and replaces whatever the club
had. From then on every AI action of the club runs on it, uncounted against
the plan (access.resolve).

The key is never returned, logged or audited: `key_last4` is all that is
shown of it, and the routes that carry it are marked sensitive for error
reports (app/core/redact.py). The test call's errors are classified, never
quoted with the key in them (AIProvider.scrub).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.api.ai import errors
from app.api.ai.models import AIUsage, ClubAIKeys
from app.api.ai.schemas import KeyIn
from app.config import settings
from app.core.ai import crypto, providers
from app.core.ai.base import AUTH, BILLING, NOT_FOUND, RATE_LIMITED, SERVER, TIMEOUT, ProviderError
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


def key_out(key: ClubAIKeys | None) -> dict:
    return {
        "connected": key is not None,
        "provider": key.provider if key else None,
        "model": key.model if key else None,
        "key_last4": key.key_last4 if key else None,
        "verified_at": key.verified_at if key else None,
        "models": providers.selectable(),
    }


def _why(kind: str, provider: str, model: str) -> str:
    who = errors.provider_name(provider)
    if kind == AUTH:
        return f"{who} refused that key. Check that you copied all of it and that it is still active."
    if kind == BILLING:
        return f"That key works, but its {who} account has no credit left. Add credit, then try again."
    if kind == RATE_LIMITED:
        return f"That key is over its rate limit or out of quota with {who} right now. Try again in a minute, or check its plan."
    if kind == NOT_FOUND:
        return f"That key can't use {model}. Pick another model, or check what the key may use."
    return f"{who} wouldn't accept a test call with that key."


async def connect(session, club, user, payload: KeyIn, *, member_id: int | None, request=None) -> ClubAIKeys:
    from app.api.audit import service as audit

    if not settings.AI_ENABLED:
        raise errors.disabled()
    if not crypto.available():
        raise errors.unavailable("Connecting your own AI key isn't available on this server yet.")
    listed = providers.selectable().get(payload.provider, [])
    model = (payload.model or "").strip() or (listed[0] if listed else "")
    if model not in listed:
        raise CustomHTTPException(
            status_code=422,
            message=f"Pick one of these models: {', '.join(listed)}.",
            errors={"model": "Not one of the models listed for this provider."},
        )
    api_key = (payload.api_key or "").strip()
    if len(api_key) < 8 or any(ch.isspace() for ch in api_key):
        raise errors.key_invalid("That doesn't look like an API key. Copy it again, all of it.", status=422)

    # Nothing held while the provider answers.
    await session.commit()
    provider = providers.make(payload.provider, api_key)
    started = time.monotonic()
    failure: ProviderError | None = None
    try:
        await provider.check_key(model=model, timeout=float(settings.AI_TIMEOUT_SECONDS))
    except ProviderError as exc:
        failure = exc
        logger.info("club %s: %s key check failed: %s %s", club.id, payload.provider, exc.kind, provider.scrub(exc.detail))
    finally:
        await provider.aclose()
    session.add(AIUsage(
        club_id=club.id, user_id=getattr(user, "id", None), member_id=member_id, action="key_check",
        provider=payload.provider, model=model, byok=True, ok=failure is None,
        error=failure.kind if failure else None, latency_ms=int((time.monotonic() - started) * 1000),
    ))
    if failure is not None:
        await session.commit()
        if failure.kind in (SERVER, TIMEOUT):
            raise errors.unavailable(f"Couldn't reach {errors.provider_name(payload.provider)} to check the key. Try again.")
        raise errors.key_invalid(_why(failure.kind, payload.provider, model), status=422)

    key = await session.scalar(select(ClubAIKeys).where(ClubAIKeys.club_id == club.id))
    now = datetime.now(timezone.utc)
    if key is None:
        key = ClubAIKeys(club_id=club.id)
        session.add(key)
    key.provider = payload.provider
    key.encrypted_key = crypto.encrypt(api_key)
    key.model = model
    key.key_last4 = api_key[-4:]
    key.verified_at = now
    key.created_by = getattr(user, "id", None)
    key.updated_at = now
    await audit.record(
        session,
        actor=user,
        action="ai_key.connect",
        entity_type="club",
        entity_id=club.id,
        summary=f"Connected its own {errors.provider_name(payload.provider)} key for AI assist",
        details={"provider": payload.provider, "model": model, "key_last4": key.key_last4},
        request=request,
    )
    await session.commit()
    return key


async def disconnect(session, club, user, *, request=None) -> None:
    from app.api.audit import service as audit

    key = await session.scalar(select(ClubAIKeys).where(ClubAIKeys.club_id == club.id))
    if key is None:
        return
    provider = key.provider
    await session.delete(key)
    await audit.record(
        session,
        actor=user,
        action="ai_key.disconnect",
        entity_type="club",
        entity_id=club.id,
        summary=f"Removed its own {errors.provider_name(provider)} key for AI assist",
        details={"provider": provider},
        request=request,
    )
    await session.commit()
