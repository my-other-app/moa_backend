"""Every club without a mailbox address gets its automatic one, made from its
name (app/api/club_mailbox/service.py backfill; docs/OPERATIONS.md section 23).
Oldest club first, so the older club wins a name; deleted clubs are skipped.
Run once after the deploy that adds the automatic addresses; safe to run
again (a second run makes nothing new).

    $DC exec -T backend python -m scripts.mailbox_backfill            # dry run: counts and the first 20, nothing kept
    $DC exec -T backend python -m scripts.mailbox_backfill --apply    # does it

The admin portal's POST /api/v1/admin/club-mailbox/backfill does the same
(`{"dry_run": false}`). Whatever the plan and APP_CLUB_MAILBOX: on a plan
without the mailbox the address is only reserved. Prints one JSON line:
{"dry_run", "created", "skipped", "failed", "sample": [{"club_id", "handle"}]}.
"""
import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.db import registry  # noqa: E402,F401  (every model, for the relationships)
from app.api.club_mailbox import service  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def run(*, apply: bool) -> dict:
    async with AsyncSessionLocal() as session:
        return await service.backfill(session, apply=apply)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="make the addresses (default: a dry run)")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(apply=args.apply)), default=str))


if __name__ == "__main__":
    main()
