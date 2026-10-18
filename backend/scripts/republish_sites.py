"""Make every published club site again, now.

    docker compose exec -T backend python scripts/republish_sites.py

Run once after deploying the hashed Content-Security-Policy (app/api/sites/
render.py, SITE_CSP), before the club-sites Worker starts sending it: a page
stored before then carries its stylesheet in the old, per-club form, which
the new policy does not name, so it would show unstyled until the nightly
refresh reached it. Safe to run again; each page is committed on its own.
Prints {"published": ..., "made": ...}.
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.api.sites import service  # noqa: E402
from app.api.sites.models import ClubSites  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def run(session) -> dict:
    club_ids = list((await session.execute(
        select(ClubSites.club_id).where(ClubSites.published_at.isnot(None)).order_by(ClubSites.club_id)
    )).scalars())
    made = 0
    for club_id in club_ids:
        if await service.republish_for_club(session, club_id):
            made += 1
    return {"published": len(club_ids), "made": made}


async def main() -> None:
    async with AsyncSessionLocal() as session:
        done = await run(session)
    print(json.dumps(done))


if __name__ == "__main__":
    asyncio.run(main())
