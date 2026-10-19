"""Whether a club may ask, on whose key, and what it has used.

`resolve` decides, in this order:

1. APP_AI_ENABLED off: 403 AI_DISABLED, and nothing is called.
2. The club has its own key and its plan has `ai_byok` (every seeded plan
   does): its actions run on that key, with its provider and model, and are
   not counted against `ai_actions_per_month`. If the key cannot be read
   (no APP_AI_KEYS_SECRET), 503 -- never the platform's key instead.
3. Otherwise the platform's key: the plan needs `ai_assist`
   (403 PLAN_FEATURE_LOCKED), and each action is counted.

`run` makes one action happen: the row in ai_usage first (on the platform's
key, under a per-club lock, after checking this month's count: 429
AI_QUOTA_REACHED when it is used up), then the call (runner.complete), then
the row finished. A row is `ok` only when the answer came back and parsed;
a failed call is logged and never counted. The request's transaction is
committed before the call, so no connection is held while a model thinks.

The plan gates' rules hold here as everywhere (subscriptions.gates): an
admin, or an admin acting as the club, is never gated or capped, and
APP_PLAN_ENFORCEMENT=false lifts the gate and the cap. AI assist is
club-wide, so no event is grandfathered for it. APP_AI_ENABLED is the switch
that stops spending.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, or_, select, text

from app.api.ai import errors
from app.api.ai.models import AIUsage, ClubAIKeys
from app.api.subscriptions import gates
from app.api.subscriptions.service import IST, month_start
from app.config import settings
from app.core.ai import crypto, providers, runner
from app.core.ai.base import AIRequest
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: An action still under way counts for this long (a process that died
#: mid-call leaves its row unfinished; after this it no longer holds a place).
PENDING_FOR = timedelta(minutes=10)
LIMIT_KEY = "ai_actions_per_month"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def resets_at(now: Optional[datetime] = None) -> datetime:
    """The first instant of next month in India: when the count starts again."""
    local = month_start(now).astimezone(IST)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return local.replace(year=year, month=month)


async def month_used(session, club_id: int, now: Optional[datetime] = None) -> int:
    """This month's actions on the platform's key: the ones that succeeded,
    and the ones under way from the last PENDING_FOR."""
    now = now or now_utc()
    return int(
        await session.scalar(
            select(func.count(AIUsage.id)).where(
                AIUsage.club_id == club_id,
                AIUsage.created_at >= month_start(now),
                AIUsage.byok == False,  # noqa: E712
                or_(AIUsage.ok == True, (AIUsage.ok.is_(None)) & (AIUsage.created_at > now - PENDING_FOR)),  # noqa: E712
            )
        )
        or 0
    )


async def club_key(session, club_id: int) -> Optional[ClubAIKeys]:
    return await session.scalar(select(ClubAIKeys).where(ClubAIKeys.club_id == club_id))


@dataclass
class Access:
    """What one club's actions run on."""

    club_id: int
    byok: bool
    provider: str
    model: Optional[str] = None
    api_key: str = field(default="", repr=False)

    def routes(self, *, vision: bool) -> list[providers.Route]:
        if self.byok:
            return [providers.club_route(self.provider, self.api_key, self.model)]
        return providers.platform_routes(vision=vision)


async def byok_allowed(session, club_id: int, user) -> bool:
    return await gates.allows(session, club_id, "ai_byok", actor=user)


async def plan_allows(session, club_id: int, user, key: Optional[ClubAIKeys]) -> bool:
    """What `status` says: the club may use AI assist, on its own key or ours."""
    if key is not None and await byok_allowed(session, club_id, user):
        return True
    return await gates.allows(session, club_id, "ai_assist", actor=user)


async def resolve(session, club_id: int, user) -> Access:
    if not settings.AI_ENABLED:
        raise errors.disabled()
    key = await club_key(session, club_id)
    if key is not None and await byok_allowed(session, club_id, user):
        try:
            plain = crypto.decrypt(key.encrypted_key)
        except crypto.Unavailable:
            logger.error("club %s has its own AI key but it cannot be decrypted (APP_AI_KEYS_SECRET)", club_id)
            raise errors.unavailable("Your club's own AI key can't be used right now. Try again later, or connect it again.")
        return Access(club_id=club_id, byok=True, provider=key.provider, model=key.model, api_key=plain)
    await gates.require_feature(session, club_id, "ai_assist", actor=user)
    if not providers.platform_routes(vision=False):
        raise errors.unavailable("AI assist isn't set up on this server yet.")
    return Access(club_id=club_id, byok=False, provider=providers.GEMINI)


async def quota_reached(session, club_id: int, *, cap: int, used: int) -> CustomHTTPException:
    """429 AI_QUOTA_REACHED, worded like the plan's other limits, with when
    the count starts again and the cheapest plan with more."""
    ent = await gates.entitlements(session, club_id)
    refusal = await gates.limit_reached(session, ent, LIMIT_KEY, cap=cap, used=used, feature="ai_assist")
    reset = resets_at()
    return CustomHTTPException(
        status_code=429,
        message=f"{refusal.message} They reset on {reset.day} {reset:%B}.",
        error_code=errors.AI_QUOTA_REACHED,
        errors={**refusal.errors, "resets_at": reset.isoformat()},
    )


async def reserve(session, access: Access, *, action: str, user, member_id: Optional[int]) -> AIUsage:
    """The action's row, written and committed before the call. On the
    platform's key, under a per-club lock with the count, so two requests at
    once cannot both take the last action."""
    if not access.byok:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"ai_quota:{access.club_id}"})
        cap = await gates.cap(session, access.club_id, LIMIT_KEY, actor=user)
        if cap is not None:
            used = await month_used(session, access.club_id)
            if used + 1 > cap:
                # The request's session ends with the refusal, and with it the lock.
                raise await quota_reached(session, access.club_id, cap=cap, used=used)
    row = AIUsage(
        club_id=access.club_id,
        user_id=getattr(user, "id", None),
        member_id=member_id,
        action=action,
        provider=access.provider,
        byok=access.byok,
        ok=None,
        created_at=now_utc(),
    )
    session.add(row)
    await session.commit()
    return row


async def run(
    session,
    access: Access,
    *,
    action: str,
    request: AIRequest,
    parse: Callable[[str], Any],
    user,
    member_id: Optional[int] = None,
) -> runner.Outcome:
    """One action, start to finish. Raises the club-facing error on failure."""
    routes = access.routes(vision=request.vision)
    if not routes:
        raise errors.unavailable("AI assist isn't set up on this server yet.")
    row = await reserve(session, access, action=action, user=user, member_id=member_id)
    try:
        outcome = await runner.complete(routes, request, parse)
    except runner.AIFailure as failure:
        row.ok = False
        row.error = failure.kind[:40]
        row.provider = failure.provider or row.provider
        row.model = failure.model or None
        row.input_tokens, row.output_tokens, row.latency_ms = failure.input_tokens, failure.output_tokens, failure.latency_ms
        await session.commit()
        raise errors.from_failure(failure, provider=access.provider)
    except BaseException:
        # A bug, or the request cancelled: the action did not happen.
        try:
            row.ok, row.error = False, "error"
            await session.commit()
        except Exception:  # noqa: BLE001 -- the original error is the one to raise
            pass
        raise
    row.ok = True
    row.provider = outcome.provider
    row.model = outcome.model
    row.input_tokens, row.output_tokens, row.latency_ms = outcome.input_tokens, outcome.output_tokens, outcome.latency_ms
    await session.commit()
    return outcome
