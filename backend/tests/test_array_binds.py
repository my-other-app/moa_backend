"""A list of ids bound as one array (app/db/binds.any_of), however long.

An IN list binds a parameter per id and the driver takes at most 32,767:
the missing-answers filters of the guest list and the Review list, and the
retention scrub, read lists that grow with an event's people."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.api.approvals import service as approvals
from app.api.events.models import EventRegistrationsLink
from app.api.users.models import Users
from app.db.binds import any_of

NOW = datetime.now(timezone.utc)


async def test_forty_thousand_ids_are_one_parameter(session, make_user):
    a = await make_user(email="binds-a@example.com")
    b = await make_user(email="binds-b@example.com")
    ids = list(range(10_000_000, 10_040_000)) + [a.id]
    assert await session.scalar(select(func.count()).select_from(Users).where(any_of(Users.id, ids))) == 1
    assert await session.scalar(select(func.count()).select_from(Users).where(any_of(Users.id, [a.id, b.id]))) == 2
    # Nothing matches nothing, as IN () does.
    assert await session.scalar(select(func.count()).select_from(Users).where(any_of(Users.id, []))) == 0


async def test_uuid_ids_too(session, make_club, make_event, make_user, make_registration):
    owner, club = await make_club(name="Binds Club", email="binds-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Binds Night", event_datetime=NOW + timedelta(days=3))
    reg = await make_registration(event=event, user=await make_user(email="binds-guest@example.com"))
    await session.flush()
    ids = [uuid.uuid4() for _ in range(40_000)] + [reg.id]
    found = (await session.scalars(select(EventRegistrationsLink.id).where(any_of(EventRegistrationsLink.id, ids)))).all()
    assert found == [reg.id]


async def test_the_review_lists_only_ids_filter_takes_a_long_list(session):
    stmt = approvals.list_query(1, status=None, q=None, only_ids=[uuid.uuid4() for _ in range(40_000)])
    assert (await session.execute(stmt)).all() == []
    # One parameter for the ids, not one each.
    compiled = stmt.compile(dialect=session.bind.dialect)
    assert len(compiled.params) < 10
