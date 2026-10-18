"""Post the money history to the ledger: every payment, registration, store
order, subscription charge and complimentary plan from before it existed.

    docker compose -f docker-compose.prod.yml exec -T backend python scripts/ledger_backfill.py

Run after the deploy that brings the ledger (migration w2_ledger), while the
app keeps taking payments: each batch is its own short transaction, nothing
is locked for longer, and a registration a live payment is posting for is
skipped and tried again. Safe to run again (every posting is keyed by what
it records, so nothing is posted twice); a run cut short is finished by the
next. Reads only our own tables; never calls Razorpay.

Prints what it saw and wrote, per walk, and how many postings failed:
{"payments": {"seen": .., "lines": ..}, "registrations": {.., "busy_skipped": ..},
 "charges": {..}, "comps": {..}, "failed": .., "seconds": ..}

Then run scripts/ledger_compare.py.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

sys.path.insert(0, ".")

from app.api.ledger import backfill  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def run(session, *, batch: int = backfill.BATCH, since: datetime | None = None) -> dict:
    """The backfill on `session` (committed batch by batch)."""
    return await backfill.backfill(session, batch=batch, since=since)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--batch", type=int, default=backfill.BATCH, help="rows a transaction (default %(default)s)")
    parser.add_argument("--since", type=datetime.fromisoformat, default=None,
                        help="only rows changed since this ISO time (default: everything)")
    args = parser.parse_args()
    async with AsyncSessionLocal() as session:
        done = await run(session, batch=args.batch, since=args.since)
    print(json.dumps(done))


if __name__ == "__main__":
    asyncio.run(main())
