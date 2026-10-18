"""Meilisearch's indexes, by hand: rebuild them, apply their settings, check them.

    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/search_reindex.py            # full rebuild
    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/search_reindex.py --settings # settings only
    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/search_reindex.py --check    # compare and repair
    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/search_reindex.py --status   # what is there

Needs APP_SEARCH_URL and APP_SEARCH_KEY (the backend's own .env). The app
builds the indexes by itself the first time it finds them missing or built
for an older document version (app/api/search/sync.py); this is for doing it
on purpose -- after restoring the database, after a Meilisearch volume was
lost, or to see the counts.

A rebuild makes each index afresh under another name from every row, applies
the settings, and swaps it in, so searches keep the old one until the new one
is whole. It takes the search sync's lock, waiting for a drain in progress;
while it runs the outbox fills, and the next drain sends what changed. Safe to
run again at any time. Prints JSON.
"""

import argparse
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.api.search import sync  # noqa: E402
from app.core import search  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402
from app.db.locks import singleton  # noqa: E402

#: How long to wait for the sync's lock (a drain, a build) before giving up.
WAIT_SECONDS = 600


async def status(session) -> dict:
    out = {"configured": search.configured(), "healthy": await search.healthy() if search.configured() else False,
           "built": await sync.is_built(session), "outbox": await session.scalar(text("SELECT count(*) FROM search_outbox"))}
    for kind in search.KINDS:
        got = await search.stats(search.index(kind)) if search.configured() else None
        out[search.index(kind)] = (got or {}).get("numberOfDocuments")
    return out


async def run(session, what: str) -> dict:
    if what == "status":
        return await status(session)
    if not search.configured():
        return {"error": "APP_SEARCH_URL is not set"}
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        async with singleton(session, sync.SEARCH_LOCK) as mine:
            if mine:
                started = time.monotonic()
                if what == "settings":
                    done = {search.index(k): await search.apply_settings(search.index(k), k) for k in search.KINDS}
                elif what == "check":
                    done = await sync.reconcile(session)
                else:
                    done = await sync.rebuild(session)
                return {what: done, "seconds": round(time.monotonic() - started, 1)}
        if time.monotonic() > deadline:
            return {"error": "the search sync's lock stayed taken; try again"}
        await asyncio.sleep(2)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--settings", action="store_const", const="settings", dest="what", help="apply the indexes' settings only")
    g.add_argument("--check", action="store_const", const="check", dest="what", help="compare the indexes with the rows and repair")
    g.add_argument("--status", action="store_const", const="status", dest="what", help="what is configured, built and indexed")
    args = p.parse_args()
    async with AsyncSessionLocal() as session:
        done = await run(session, args.what or "rebuild")
    print(json.dumps(done, default=str))


if __name__ == "__main__":
    asyncio.run(main())
