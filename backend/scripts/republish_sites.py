"""Make every published club site again, now.

    docker compose exec -T backend python scripts/republish_sites.py

Run once after deploying a change to the website templates or their
renderer (app/api/sites: the v2 templates of migration site_templates_v2, a
template's stylesheet, the hashed Content-Security-Policy): a page stored
before then carries the old markup and stylesheet, which the new policy may
not name, so it would show unstyled until the sites sweep or the nightly
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
