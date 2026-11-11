"""Requests, writes, errors and attendee reads per client, club and day (India's day).

One upsert per request, after its answer. What the club dashboard's Usage
panel and the admin's platform view read.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.partner.common import IST, now
from app.api.partner.models import PartnerUsageDaily

logger = logging.getLogger(__name__)


def today() -> date:
    return now().astimezone(IST).date()


async def record(
    session, *, client_id: str, client_type: str, club_id: Optional[int], livemode: bool, status: int, write: bool,
    attendee_reads: int = 0,
) -> None:
    table = PartnerUsageDaily.__table__
    values = dict(
        day=today(), client_id=client_id, club_id=club_id or 0, client_type=client_type, livemode=livemode,
        requests=1, writes=1 if write else 0, errors=1 if status >= 400 else 0, rate_limited=1 if status == 429 else 0,
        attendee_reads=int(attendee_reads or 0),
    )
    stmt = pg_insert(table).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.day, table.c.client_id, table.c.club_id],
        set_={
            "requests": table.c.requests + 1,
            "writes": table.c.writes + stmt.excluded.writes,
            "errors": table.c.errors + stmt.excluded.errors,
            "rate_limited": table.c.rate_limited + stmt.excluded.rate_limited,
            "attendee_reads": table.c.attendee_reads + stmt.excluded.attendee_reads,
        },
    )
    try:
        if status >= 400 and session.in_transaction():
            await session.rollback()
        await session.execute(stmt)
        await session.commit()
    except Exception:  # noqa: BLE001 - counting never costs a caller their answer
        logger.warning("could not count a partner API request", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


async def by_day(session, *, club_id: Optional[int] = None, days: int = 30) -> list[dict]:
    """Each of the last `days` days, oldest first, with zeros where nothing happened."""
    since = today() - timedelta(days=days - 1)
    U = PartnerUsageDaily
    q = select(
        U.day, func.sum(U.requests), func.sum(U.writes), func.sum(U.errors), func.sum(U.rate_limited), func.sum(U.attendee_reads),
    ).where(U.day >= since).group_by(U.day)
    if club_id is not None:
        q = q.where(U.club_id == club_id)
    found = {row[0]: row for row in (await session.execute(q)).all()}
    out = []
    for i in range(days):
        d = since + timedelta(days=i)
        row = found.get(d)
        out.append({
            "day": d.isoformat(),
            "requests": int(row[1]) if row else 0,
            "writes": int(row[2]) if row else 0,
            "errors": int(row[3]) if row else 0,
            "rate_limited": int(row[4]) if row else 0,
            "attendee_reads": int(row[5]) if row else 0,
        })
    return out


async def by_client(session, *, club_id: Optional[int] = None, days: int = 30) -> list[dict]:
    since = today() - timedelta(days=days - 1)
    U = PartnerUsageDaily
    q = select(
        U.client_id, U.client_type, U.club_id, func.sum(U.requests), func.sum(U.errors), func.max(U.day),
    ).where(U.day >= since).group_by(U.client_id, U.client_type, U.club_id).order_by(func.sum(U.requests).desc()).limit(200)
    if club_id is not None:
        q = q.where(U.club_id == club_id)
    return [
        {"client_id": c, "client_type": t, "club_id": k or None, "requests": int(r or 0), "errors": int(e or 0), "last_day": d.isoformat() if d else None}
        for c, t, k, r, e, d in (await session.execute(q)).all()
    ]
