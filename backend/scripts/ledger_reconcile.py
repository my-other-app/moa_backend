"""The ledger's daily checks, by hand.

    docker compose -f docker-compose.prod.yml exec -T backend python scripts/ledger_reconcile.py --sums [--full]
    docker compose -f docker-compose.prod.yml exec -T backend python scripts/ledger_reconcile.py --razorpay [--day 2026-10-17]

--sums: ledger_monthly and ledger_platform_monthly against the entries,
corrected where they drifted (every club with --full; otherwise those with
entries since the daily job last ran). --razorpay: one IST day's payments and
refunds at Razorpay, every account, against the ledger (read-only towards
Razorpay; default yesterday). Each tells the admins what it found, as the
daily job does, and prints it.
"""

import argparse
import asyncio
import json
import sys
from datetime import date

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.api.ledger import jobs, recon  # noqa: E402
from app.api.models import ScheduledRuns  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sums", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--razorpay", action="store_true")
    parser.add_argument("--day", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    out = {}
    async with AsyncSessionLocal() as session:
        if args.sums:
            if args.full:
                out["sums"] = await jobs.reconcile(session)
            else:
                last = await session.scalar(select(ScheduledRuns.result).where(ScheduledRuns.name == jobs.JOB))
                watermark = int((last or {}).get("watermark") or 0)
                clubs = await jobs._clubs_since(session, watermark)
                months = await jobs._months_since(session, watermark)
                out["sums"] = await jobs.reconcile(session, clubs=clubs, months=months)
        if args.razorpay:
            out["razorpay"] = await recon.reconcile_day(session, args.day)
    print(json.dumps(jobs._jsonable(out)))


if __name__ == "__main__":
    asyncio.run(main())
