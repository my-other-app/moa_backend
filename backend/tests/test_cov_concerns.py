"""Behaviour pinned where the feature register flagged a speed or exposure
concern, without rewriting anything: People Like You answers in a fixed
number of statements however many people are checked in (it returns them
all, unpaged), and GET /orgs/list is the organisation picker every client
reads."""
from contextlib import contextmanager

import pytest
from sqlalchemy import event as sa_event

from app.api.orgs.models import Organizations, OrgTypes
from app.api.users.models import UserInterests, UserProfiles
from tests.test_networking import _fetch, world  # noqa: F401 (fixture)


@contextmanager
def counted(session):
    seen: list[str] = []
    conn = session.bind.sync_connection

    def grab(_conn, _cursor, statement, _parameters, _context, _executemany):
        seen.append(statement)

    sa_event.listen(conn, "before_cursor_execute", grab)
    try:
        yield seen
    finally:
        sa_event.remove(conn, "before_cursor_execute", grab)


async def _more_guests(session, make_user, make_registration, event, n, start):
    from sqlalchemy import select

    from app.api.interests.models import Interests

    ai = await session.scalar(select(Interests).where(Interests.name == "AI"))
    for i in range(start, start + n):
        user = await make_user(email=f"cov-net-{i}@example.com")
        user.full_name = f"Guest {i:03d}"
        session.add(UserProfiles(user_id=user.id, full_name=user.full_name))
        session.add(UserInterests(user_id=user.id, interest_id=ai.id))
        await make_registration(event=event, user=user, is_attended=True)
    await session.commit()


async def test_people_like_you_costs_the_same_statements_for_ten_or_forty_people(client, session, world, make_user, make_registration):  # noqa: F811
    await _more_guests(session, make_user, make_registration, world["event"], 7, 0)
    with counted(session) as small:
        r = await _fetch(client, world)
    assert r.status_code == 200 and r.json()["total"] == 10
    await _more_guests(session, make_user, make_registration, world["event"], 30, 100)
    with counted(session) as big:
        r = await _fetch(client, world)
    body = r.json()
    # Everyone, in one answer: it is not paged (the register's concern at a 5,000-person event).
    assert body["total"] == 40 and len(body["attendees"]) == 40
    # Most shared first, then by name: the guests sharing AI follow Dev, before those sharing nothing.
    assert body["attendees"][0]["name"] == "Dev" and body["attendees"][-1]["name"] == "Chirag"
    # No statement per person: the rows grow, the statements do not.
    assert len(big) == len(small), (len(small), len(big))


async def test_the_organisation_picker_lists_every_live_organisation(client, session, make_user, mint_token):
    from tests.conftest import auth_header

    coep = Organizations(name="COEP Tech", type=OrgTypes.college, email="office@coep.example")
    gone = Organizations(name="Closed College", type=OrgTypes.college, is_deleted=True)
    held = Organizations(name="Blocked Body", type=OrgTypes.community, is_blocked=True)
    session.add_all([coep, gone, held])
    await session.commit()
    person = await make_user(email="cov-orgs@example.com")
    signed_in = await client.get("/api/v1/orgs/list", headers=auth_header(await mint_token(person)))
    assert signed_in.status_code == 200
    rows = {o["name"]: o for o in signed_in.json()}
    assert "COEP Tech" in rows and "Blocked Body" in rows and "Closed College" not in rows
    assert rows["Blocked Body"]["is_blocked"] is True and rows["COEP Tech"]["type"] == "college"
    assert set(rows["COEP Tech"]) == {"id", "name", "type", "address", "phone", "email", "website", "logo", "is_blocked"}
    # As it stands it needs no sign-in (every client that reads it is signed in; see the PR).
    signed_out = await client.get("/api/v1/orgs/list")
    assert signed_out.status_code == 200 and {o["name"] for o in signed_out.json()} == set(rows)


@pytest.mark.parametrize("path", ["/api/v1/orgs/create", "/api/v1/orgs/update/1", "/api/v1/orgs/delete/1"])
async def test_changing_an_organisation_needs_an_admin(client, path):
    method = {"create": "post", "update": "put", "delete": "delete"}[path.split("/")[4]]
    assert (await getattr(client, method)(path)).status_code == 401
