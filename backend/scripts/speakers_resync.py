"""Speaker profiles that a club's earlier edits never reached
(app/api/speakers/links.py, resync_club_profiles). Until event saves copied
a club's edits onto the profile that is the club's alone, an edit stopped at
the event's own copy, and the speaker's page kept what the club had put
right. Run once after the deploy that adds follow_club_edits; safe to run
again (a second run changes nothing).

    $DC exec -T backend python -m scripts.speakers_resync            # dry run: the counts, nothing kept
    $DC exec -T backend python -m scripts.speakers_resync --apply    # does it

Only active, unclaimed profiles on one club's events, from the event row the
club changed last, when it changed after the profile did. Prints the counts
as one JSON line.
"""
import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.db import registry  # noqa: E402,F401  (every model, for the relationships)
from app.api.speakers import links  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def run(*, apply: bool) -> dict:
    async with AsyncSessionLocal() as session:
        return await links.resync_club_profiles(session, apply=apply)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="copy and keep (default: a dry run)")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(apply=args.apply)), default=str))


if __name__ == "__main__":
    main()
