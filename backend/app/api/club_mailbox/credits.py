"""What a mail the club sends costs: one EMAIL credit, and how many are left.

The contract with the message quotas (app/api/message_quota), which another
change is turning into prepaid credits:

- before a send, `message_quota.check(session, club_id, "email", KIND)`:
  the club's terms and why this send would be held back (None: it may go).
  A reason refuses the send: "Buy more email credits".
- after the provider took it, `message_quota.record_message(club_id,
  "email", KIND, via="platform", session=..., ref=...)`: one credit, once
  per mail (the ref makes a retried record count once).
- how many are left: the credits module's `remaining(session, club_id,
  channel)` when it exists; until then, the plan's monthly email allowance
  less this month's use (None: unlimited).
"""
from __future__ import annotations

import importlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

KIND = "mailbox"
CHANNEL = "email"
#: Where the prepaid credits' `remaining` may live, first found wins.
CREDIT_MODULES = (
    "app.api.message_quota.credits",
    "app.api.message_credits.credits",
    "app.api.message_credits.service",
    "app.api.message_credits",
    "app.api.credits",
)
BUY_MORE = "You're out of email credits. Buy more email credits to send from the club's mailbox."


def _credits_remaining():
    for name in CREDIT_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        fn = getattr(module, "remaining", None)
        if callable(fn):
            return fn
    return None


async def remaining(session, club_id: int) -> Optional[int]:
    """Email credits left (None: no limit)."""
    fn = _credits_remaining()
    if fn is not None:
        value = await fn(session, club_id, CHANNEL)
        # The credits module answers its UNLIMITED sentinel for a plan with no limit.
        unlimited = getattr(importlib.import_module(fn.__module__), "UNLIMITED", None)
        return None if value is None or (unlimited is not None and value >= unlimited) else max(0, int(value))
    from sqlalchemy import select

    from app.api.message_quota import service as quota
    from app.api.message_quota.models import MessageUsage

    terms = await quota.terms_for(session, club_id)
    limit = terms.limit(CHANNEL)
    if limit is None:
        return None
    used = await session.scalar(
        select(MessageUsage.used).where(
            MessageUsage.club_id == club_id, MessageUsage.period == quota.period_of(), MessageUsage.channel == CHANNEL,
        )
    )
    return max(0, int(limit) - int(used or 0))


async def held_back(session, club_id: int) -> Optional[str]:
    """Why a send is refused now, or None. The quotas' own hold-back first;
    then, when the prepaid credits exist, none left."""
    from app.api.message_quota import service as quota

    _terms, reason = await quota.check(session, club_id, CHANNEL, KIND)
    if reason:
        return reason
    fn = _credits_remaining()
    if fn is not None:
        left = await fn(session, club_id, CHANNEL)
        if left is not None and int(left) <= 0:
            return BUY_MORE
    return None


async def record(session, club_id: int, message_id: int) -> None:
    """One credit for the mail the provider took. Never raises (a mail that
    went is not reported failed for a count that couldn't be written)."""
    from app.api.message_quota import service as quota

    try:
        async with session.begin_nested():
            await quota.record_message(club_id, CHANNEL, KIND, via=quota.PLATFORM, session=session, ref=f"mailbox:{message_id}")
    except Exception:  # noqa: BLE001
        logger.warning("club %s: a mailbox send could not be counted", club_id, exc_info=True)
