"""Store again every club's place that disagrees with its rows.

    docker compose exec -T backend python scripts/recompute_club_places.py

A club's place (clubs.place_lat / place_lng: its own point, else the middle of
its in-person events) is kept by triggers on clubs and events
(app/api/discover/places.py). Run this after anything that wrote clubs or
events with triggers switched off (a data-only restore with
--disable-triggers, session_replication_role = replica, a bulk load), or once
after deploying the migration discover_scale, to put right any club a write
made while the migration's backfill ran. The same as
POST /api/v1/admin/discover/recompute-club-places.

One pass over the events with a point; writes only the clubs whose stored
place is wrong. Safe to run again. Prints {"checked": [...], "disagreed": n,
"fixed": n}: the first 50 ids that disagreed, how many did, and how many
were rewritten.
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.api.discover import places  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def run(session) -> dict:
    """The recompute on `session` (committed): which clubs disagreed, and how many were fixed."""
    wrong = await places.drift(session)
    fixed = await places.recompute(session) if wrong else 0
    return dict(checked=wrong[:50], disagreed=len(wrong), fixed=fixed)


async def main() -> None:
    async with AsyncSessionLocal() as session:
        done = await run(session)
    print(json.dumps(done))


if __name__ == "__main__":
    asyncio.run(main())
