"""events.going_count and clubs.follower_count, checked against the rows.

Triggers keep both exact in the writer's own transaction (see GOING_COUNT_SQL
in app/api/events/models.py and FOLLOWER_COUNT_SQL in app/api/clubs/models.py).
What can still put them out: something that writes with triggers switched off
(a data-only restore with --disable-triggers, session_replication_role =
replica) or edits a counter by hand. `reconcile` finds any row whose counter
disagrees with its rows and recounts it; the closeout sweep runs it every few
hours and an admin can run it at any time.

Each disagreeing row is recounted while holding that row's lock: a
registration or follow in flight either finishes first (and is counted) or
waits for the recount and then adds itself, so a reconcile running alongside
writes cannot itself leave a counter wrong.
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import text

# A counter differs from what its rows say.
_GOING_DRIFT = """
SELECT e.id FROM events e
LEFT JOIN (SELECT event_id, count(*) AS n FROM event_registrations_link WHERE NOT is_deleted GROUP BY event_id) c ON c.event_id = e.id
WHERE e.going_count <> coalesce(c.n, 0)
"""
_FOLLOWER_DRIFT = """
SELECT k.id FROM clubs k
LEFT JOIN (SELECT club_id, count(*) AS n FROM club_users_link WHERE is_following AND NOT is_deleted GROUP BY club_id) c ON c.club_id = k.id
WHERE k.follower_count <> coalesce(c.n, 0)
"""
_GOING_FIX = """
UPDATE events SET going_count = (
  SELECT count(*) FROM event_registrations_link r WHERE r.event_id = events.id AND NOT r.is_deleted
) WHERE id = :id
"""
_FOLLOWER_FIX = """
UPDATE clubs SET follower_count = (
  SELECT count(*) FROM club_users_link f WHERE f.club_id = clubs.id AND f.is_following AND NOT f.is_deleted
) WHERE id = :id
"""

EVERY_SECONDS = 6 * 3600
_last_run: Optional[float] = None


async def drift(session) -> dict:
    """The ids whose counter disagrees with its rows, without changing anything."""
    return dict(
        events=list((await session.execute(text(_GOING_DRIFT))).scalars().all()),
        clubs=list((await session.execute(text(_FOLLOWER_DRIFT))).scalars().all()),
    )


async def reconcile(session) -> dict:
    """Recount every counter that disagrees with its rows; returns how many were put right."""
    found = await drift(session)
    for table, fix, ids in (("events", _GOING_FIX, found["events"]), ("clubs", _FOLLOWER_FIX, found["clubs"])):
        for row_id in ids:
            await session.execute(text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"), {"id": row_id})
            await session.execute(text(fix), {"id": row_id})
    await session.commit()
    return dict(events=len(found["events"]), clubs=len(found["clubs"]))


def due(now: Optional[float] = None) -> bool:
    """Whether the sweep should reconcile now (every EVERY_SECONDS in this process)."""
    global _last_run
    now = time.monotonic() if now is None else now
    if _last_run is not None and now - _last_run < EVERY_SECONDS:
        return False
    _last_run = now
    return True
