"""Every event speaker row joins a profile in the speakers directory
(app/api/speakers/backfill.py, docs/SPEAKERS.md). Run once after the deploy
that adds the directory; safe to run again (a second run links nothing new).

    $DC exec -T backend python -m scripts.speakers_backfill            # dry run: the counts, nothing kept
    $DC exec -T backend python -m scripts.speakers_backfill --apply    # does it

The admin portal's POST /api/v1/admin/speakers/backfill queues the same work
as a job. Nothing any page shows changes: each event keeps its own copy of
its speakers. Prints the counts as one JSON line.
"""
import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.db import registry  # noqa: E402,F401  (every model, for the relationships)
from app.api.speakers import backfill  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def run(*, apply: bool) -> dict:
    async with AsyncSessionLocal() as session:
        return await backfill.run(session, apply=apply)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="link and keep (default: a dry run)")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(apply=args.apply)), default=str))


if __name__ == "__main__":
    main()
