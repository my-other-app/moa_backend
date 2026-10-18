"""The finance and revenue figures both ways -- worked out from the rows, and
read from the ledger -- per club and month, and every difference.

    docker compose -f docker-compose.prod.yml exec -T backend python scripts/ledger_compare.py [--events] [--club ID ...]

Run after scripts/ledger_backfill.py, before setting APP_LEDGER_READS=true.
Read-only. Prints a summary line and each difference as a JSON line
({"club_id", "month", "what", "rows", "ledger"}); exits 1 when there is any.

A difference is real money the two ways see differently. The known kinds
(app/api/ledger/compare.py): a paid registration deleted afterwards (the
rows forget its money, the ledger keeps it), and on an event's own page
(--events) a ticket paid in a later month than its registration (the
ledger dates it by the payment).
"""

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.api.ledger import compare  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--events", action="store_true", help="every event's own page too")
    parser.add_argument("--club", type=int, action="append", help="only this club (repeatable)")
    args = parser.parse_args()
    async with AsyncSessionLocal() as session:
        done = await compare.compare(session, club_ids=args.club, events=args.events)
    diffs = done["differences"]
    print(json.dumps({"clubs": done["clubs"], "club_months": done["club_months"], "differences": len(diffs)}))
    for d in diffs:
        print(json.dumps(d, default=str))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
