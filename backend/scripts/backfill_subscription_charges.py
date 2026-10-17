"""Fill the revenue ledger (subscription_charges) from Razorpay.

    docker compose exec -T backend python scripts/backfill_subscription_charges.py

Run once after deploying the ledger: payments Razorpay took before it existed
are only in Razorpay. The same as POST /api/v1/admin/revenue/backfill.

Read-only towards Razorpay (it lists invoices and reads payments; it never
creates, cancels or refunds anything). For every Razorpay subscription a club
holds, or held before a complimentary plan replaced it, each paid invoice's
payment is added to the ledger unless it is there already, in which case only
its refunds are brought up to date. Safe to run again. Prints what it did:
{"scanned": ..., "inserted": ..., "updated": ..., "failed": ...}.
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.api.subscriptions import ledger  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402


async def run(session, client=None) -> dict:
    """The backfill on `session` (committed), against `client` (Razorpay's
    unless given -- the tests pass a fake)."""
    return await ledger.backfill(session, client=client)


async def main() -> None:
    async with AsyncSessionLocal() as session:
        done = await run(session)
    print(json.dumps(done))


if __name__ == "__main__":
    asyncio.run(main())
