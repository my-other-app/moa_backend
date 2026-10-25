"""The hourly billing sweep's message steps. Overage billing is retired.

Until October 2026, a month's messages past a plan's limits were billed
after the fact: the sweep closed each month into a `message_statements`
row and added the extra to the club's Razorpay subscription as an add-on.
The first such charge was due on the first sweep after 1 November 2026
(IST). Clubs now buy prepaid credits instead (credits.py), so nothing past
a plan's allowance is ever charged after the fact, and that code is gone.

What is left (`run`, from app/api/subscriptions/sweep.py):

1. `void_open`: any statement still waiting to be charged (pending,
   charging, failed, unconfirmed, gave up) or left for an admin to invoice
   by hand (manual) is made `void` -- never charged. The migration
   message_credits did this once; this makes sure of it every run. No
   statement is made any more, so there is normally nothing to do.
2. The credit steps (credits.run): the 30-day expiry notices, and
   checkouts whose payment webhook never came.
3. `prune_refs`: counted messages' idempotency keys older than REF_DAYS.

`settings.MESSAGE_OVERAGE_BILLING` is read by nothing: whatever it says --
or if it is not set at all -- nothing is charged. It is kept only so an
`.env` that still sets it starts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, update

from app.api.message_quota import credits, service
from app.api.message_quota.models import MessageStatements, MessageUsageRefs, StatementStatus as S

logger = logging.getLogger(__name__)

#: The statements the retired billing could still have charged or invoiced.
OPEN = (S.PENDING, S.CHARGING, S.FAILED, S.UNCONFIRMED, S.GAVE_UP, S.MANUAL)
#: What a voided statement says.
VOIDED = "Overage billing was retired for prepaid credits: never charged"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def void_open(session, now: Optional[datetime] = None) -> int:
    """Every statement the retired billing could still charge: `void`.
    Returns how many. Commits."""
    now = now or _now()
    res = await session.execute(
        update(MessageStatements)
        .where(MessageStatements.status.in_(OPEN))
        .values(status=S.VOID, next_attempt_at=None, charging_since=None, last_error=VOIDED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    voided = int(res.rowcount or 0)
    if voided:
        logger.warning("message statements: %s left from the retired overage billing made void", voided)
    return voided


async def prune_refs(session, now: Optional[datetime] = None) -> int:
    now = now or _now()
    res = await session.execute(
        delete(MessageUsageRefs).where(MessageUsageRefs.created_at < now - timedelta(days=service.REF_DAYS))
    )
    await session.commit()
    return int(res.rowcount or 0)


async def run(session, now: Optional[datetime] = None) -> dict:
    """The sweep's message steps. Returns what each did. The overage keys
    stay, always 0, for whatever reads the sweep's answer."""
    now = now or _now()
    out = {"message_statements": 0, "overage_charged": 0, "overage_failed": 0}
    out["statements_voided"] = await void_open(session, now)
    out.update(await credits.run(session, now))
    await prune_refs(session, now)
    return out
