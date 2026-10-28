"""The people still waiting in line at events that review every registration
become registrations under review (docs/APPROVALS.md §8, docs/OPERATIONS.md).

    $DC exec -T backend python -m scripts.waitlist_to_review            # dry run: what would happen, per event
    $DC exec -T backend python -m scripts.waitlist_to_review --apply    # does it, and queues one email each

Since the review-wording release, switching review on converts the event's
waiting list in the same transaction (events.service.update_event ->
approvals.from_waiting_list). Events switched on before that kept their
line, and nobody in it is ever offered anything (the line is off on such an
event). This runs the same function for each of them: every `waiting`
entry becomes a pending request in its turn (submitted when they joined),
the entry closes (`removed`), and the "thanks for registering" email is
queued once per new request, for the job worker to send.

- Events: review on, not deleted, cancelled, closed, archived or over, with
  anyone `waiting` in their line (approvals.waiting_list_events).
- One transaction per event, the event's row held first (as the switch and
  the line's own fill take it), review re-checked under it.
- Safe to run again: a second run finds nobody waiting and changes nothing.
  Nobody becomes two requests: someone with an open request or a live
  registration on the event just has their entry closed.
- Kept in line, untouched and reported: entries with no email to write to
  (phone only, or an address longer than a registration takes).
- The dry run does the work in a transaction and rolls it back, so its
  counts are exactly what --apply would do now.

Prints one JSON line per event, then the totals.
"""

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.db import registry  # noqa: E402,F401  (every model, for the relationships)
from app.api.approvals import notify  # noqa: E402,F401  (the email job's handler, for its priority)
from app.api.approvals import service as approvals  # noqa: E402
from app.api.events.models import Events  # noqa: E402
from app.db.core import AsyncSessionLocal  # noqa: E402

KEYS = ("moved", "already_under_review", "already_registered", "kept_no_email")


async def one(session, event_id: int, *, apply: bool) -> dict:
    """The conversion for one event, committed when `apply`, else rolled back."""
    try:
        event = await session.scalar(
            select(Events).where(Events.id == event_id).with_for_update().execution_options(populate_existing=True)
        )
        if event is None or event.is_deleted or not event.requires_approval:
            await session.rollback()
            return {"event_id": event_id, "skipped": "review is off, or the event is gone", **{k: 0 for k in KEYS}}
        name = event.name
        done = await approvals.from_waiting_list(session, event)
        if apply:
            await session.commit()
        else:
            await session.rollback()
        return {"event_id": event_id, "name": name, **done}
    except Exception:
        await session.rollback()
        raise


async def run(*, apply: bool) -> dict:
    async with AsyncSessionLocal() as session:
        ids = await approvals.waiting_list_events(session)
        await session.rollback()
        totals = {k: 0 for k in KEYS}
        for event_id in ids:
            done = await one(session, event_id, apply=apply)
            print(json.dumps(done, default=str))
            for k in KEYS:
                totals[k] += int(done.get(k) or 0)
    return {"applied": apply, "events": len(ids), **totals}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="convert and queue the emails (default: a dry run)")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(apply=args.apply))))


if __name__ == "__main__":
    main()
