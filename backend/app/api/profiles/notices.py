""""Ananya saved your profile at DevFest": the saved person's push, batched
over ten minutes per (person, event).

A save upserts the open batch for (person, event) -- one row while
`sent_at` is null (the partial unique index ux_network_save_notices_open,
"no event" included) -- adding the saver once, and writes the batch's job
for its `due_at`, ten minutes after its first save (the job's dedupe key is
the row's, so the second save writes nothing). Saving twice is saving once.

The job claims the row (`sent_at`, under FOR UPDATE) in the transaction
that decides what to say, so a second run of it sends nothing. It leaves
out savers who since removed their save, are deleted, suspended or hidden,
or are blocked either way; nothing goes when the person turned
`notify_saves` off or nobody is left. Push type `network_saves`, data
{type, event_id}.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.profiles import access, service
from app.api.profiles.models import NetworkPeople, NetworkSaveNotices
from app.core import jobs

logger = logging.getLogger(__name__)

NOTICE_JOB = "network.save_notice"
BATCH = timedelta(minutes=10)
PUSH_TYPE = "network_saves"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def note_save(session, *, person_id: int, saver_id: int, event_id: Optional[int]) -> None:
    """Add a save to the person's open batch for the event (made if none),
    and its job. In the saver's transaction."""
    N = NetworkSaveNotices
    one = func.jsonb_build_array(saver_id)
    stmt = (
        pg_insert(N)
        .values(person_id=person_id, event_id=event_id, saver_ids=[saver_id], due_at=_now() + BATCH)
        .on_conflict_do_update(
            index_elements=[N.person_id, func.coalesce(N.event_id, 0)],
            index_where=N.sent_at.is_(None),
            set_={"saver_ids": case((N.saver_ids.op("@>")(one), N.saver_ids), else_=N.saver_ids.op("||")(one))},
        )
        .returning(N.id, N.due_at)
    )
    notice_id, due_at = (await session.execute(stmt)).one()
    await jobs.enqueue(session, NOTICE_JOB, {"notice_id": notice_id}, run_at=due_at, dedupe_key=f"{NOTICE_JOB}:{notice_id}")


def wording(first_name: str, others: int, event_name: Optional[str]) -> str:
    who = first_name if others == 0 else f"{first_name} and {others} other{'s' if others != 1 else ''}"
    where = f" at {event_name}" if event_name else ""
    return f"{who} saved your profile{where}"


async def send(session, notice_id: int) -> bool:
    """Send one batch, once. True when a push went."""
    from app.api.events.models import Events

    N, NP = NetworkSaveNotices, NetworkPeople
    row = await session.scalar(select(N).where(N.id == notice_id).with_for_update().execution_options(populate_existing=True))
    if row is None or row.sent_at is not None:
        await session.commit()
        return False
    row.sent_at = _now()
    person = await access.load_person(session, user_id=row.person_id)
    if person is None or access.is_hidden(person) or not access.settings_of(person.profile)["notify_saves"]:
        await session.commit()
        return False
    wanted = [int(i) for i in (row.saver_ids or [])]
    still = set((await session.scalars(
        select(NP.owner_id).where(NP.person_id == person.id, NP.owner_id.in_(wanted), *access.shown_person_where(NP.owner_id, person.id))
    )).all()) if wanted else set()
    savers = [i for i in wanted if i in still]
    event = await session.scalar(select(Events).where(Events.id == row.event_id)) if row.event_id else None
    if not savers:
        await session.commit()
        return False
    first = (await service.people(session, [savers[0]])).get(savers[0])
    name = (service.full_name_of(first) if first is not None else "Someone").split(" ")[0] or "Someone"
    title = wording(name, len(savers) - 1, event.name if event is not None else None)
    await session.commit()
    data = {"type": PUSH_TYPE}
    if row.event_id:
        data["event_id"] = str(row.event_id)
    from app.core.notifications.service import send_notification_to_user

    try:
        return await send_notification_to_user(session, person.id, title, "Open your Network to see who and save them back.", data=data)
    except Exception:  # noqa: BLE001 - claimed: a push that failed is not sent twice
        logger.warning("network save notice %s: the push could not be sent", notice_id, exc_info=True)
        return False


@jobs.handler(NOTICE_JOB, max_attempts=5, timeout=60.0, priority=jobs.HIGH)
async def _notice_job(p: dict) -> None:
    async with jobs.scope() as s:
        await send(s, int(p["notice_id"]))
