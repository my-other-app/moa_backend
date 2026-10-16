"""Networking at an event: a checked-in guest sees the other checked-in guests,
the ones sharing most interests first, during the event and an hour after."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_user, make_event, make_registration, mint_token):
    from app.api.interests.models import InterestCategory, Interests
    from app.api.orgs.models import Organizations, OrgTypes
    from app.api.users.models import UserInterests, UserProfiles

    owner, club = await make_club(name="Circuit Society", email="net-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=NOW - timedelta(hours=1), duration=3.0)

    category = InterestCategory(name="Tech")
    session.add(category)
    await session.flush()
    ai, robots, music = (Interests(name=n, icon="•", category_id=category.id) for n in ("AI", "Robotics", "Music"))
    session.add_all([ai, robots, music])
    coep = Organizations(name="COEP Tech", type=OrgTypes.college)
    session.add(coep)
    await session.flush()

    async def guest(email, name, interests, *, attended=True, org=None):
        user = await make_user(email=email)
        user.full_name = name
        session.add(UserProfiles(user_id=user.id, full_name=name, org_id=org.id if org else None))
        session.add_all([UserInterests(user_id=user.id, interest_id=i.id) for i in interests])
        await make_registration(event=event, user=user, is_attended=attended)
        return user

    me = await guest("me@example.com", "Asha", [ai, robots], org=coep)
    twin = await guest("twin@example.com", "Dev", [ai, robots], org=coep)
    half = await guest("half@example.com", "Bina", [ai, music])
    await guest("none@example.com", "Chirag", [music])
    await guest("away@example.com", "Esha", [ai, robots], attended=False)
    gone = await guest("gone@example.com", "Farah", [ai, robots])
    gone.is_deleted = True  # an account deleted since check-in is not shown
    await session.commit()
    token = await mint_token(me)
    return dict(event=event, me=me, twin=twin, half=half, h=auth_header(token), session=session)


async def _fetch(client, world):
    # A request starts with nothing loaded, as in production: anything the
    # endpoint reads lazily must load from the database, not from this test.
    world["session"].expunge_all()
    return await client.get(f"/api/v1/events/{world['event'].id}/networking", headers=world["h"])


async def test_a_checked_in_guest_sees_the_others_most_shared_interests_first(client, world):
    r = await _fetch(client, world)
    assert r.status_code == 200, r.text
    body = r.json()
    names = [a["name"] for a in body["attendees"]]
    assert names == ["Dev", "Bina", "Chirag"]  # Esha never came in, Farah's account is gone, Asha is me
    dev = body["attendees"][0]
    assert dev["match_count"] == 2 and dev["organization"] == "COEP Tech"
    assert sorted(i["name"] for i in dev["shared_interests"]) == ["AI", "Robotics"]
    assert body["attendees"][2]["match_count"] == 0 and body["total"] == 3


async def test_a_guest_not_checked_in_is_refused(client, world, session, make_user, make_registration, mint_token):
    outsider = await make_user(email="outsider@example.com")
    await make_registration(event=world["event"], user=outsider, is_attended=False)
    await session.commit()
    world["h"] = auth_header(await mint_token(outsider))
    r = await _fetch(client, world)
    assert r.status_code == 403 and "checked in" in r.json()["message"]


async def test_the_window_closes_an_hour_after_the_event(client, world, session):
    world["event"].event_datetime = NOW - timedelta(hours=5)  # 3h event + 1h buffer ended an hour ago
    await session.commit()
    r = await _fetch(client, world)
    assert r.status_code == 403 and "closed" in r.json()["message"]
