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


async def test_each_guest_shows_their_photo_or_else_the_avatar_they_picked(client, world, session):
    """The photo they uploaded first, as everywhere else (reports.avatar);
    without one, the avatar they picked -- it used to be no picture at all."""
    from sqlalchemy import select

    from app.api.users.models import UserAvatars, UserProfiles

    def key(model, column, name):
        return f"{model.__table__.c[column].type.base_path.rstrip('/')}/{name}"

    fox = UserAvatars(name="Fox", image=key(UserAvatars, "image", f"v-{'a' * 32}.png"))
    session.add(fox)
    await session.flush()
    dev = await session.scalar(select(UserProfiles).where(UserProfiles.user_id == world["twin"].id))
    dev.avatar_id = fox.id
    bina = await session.scalar(select(UserProfiles).where(UserProfiles.user_id == world["half"].id))
    bina.avatar_id = fox.id
    bina.profile_pic = key(UserProfiles, "profile_pic", f"v-{'b' * 32}.png")
    await session.commit()

    r = await _fetch(client, world)
    assert r.status_code == 200, r.text
    pictures = {a["name"]: a["profile_pic_url"] for a in r.json()["attendees"]}
    assert f"avatars/v-{'a' * 32}.medium.png" in pictures["Dev"]
    assert f"profile_pic/v-{'b' * 32}.medium.png" in pictures["Bina"]  # the photo wins over the avatar
    assert pictures["Chirag"] is None


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


# ─── Who shows: the switch and blocks ─────────────────────────────────────


async def _names(client, world) -> list[str]:
    r = await _fetch(client, world)
    assert r.status_code == 200, r.text
    return [a["name"] for a in r.json()["attendees"]]


async def test_someone_hidden_is_never_listed(client, world, session):
    world["twin"].networking_visible = False
    await session.commit()
    body = (await _fetch(client, world)).json()
    assert [a["name"] for a in body["attendees"]] == ["Bina", "Chirag"]
    # Counts are of who is shown: Bina shares one interest, Chirag none.
    assert body["total"] == 2 and body["matches"] == 1


async def test_someone_hidden_still_sees_everyone_else(client, world, session):
    world["me"].networking_visible = False
    await session.commit()
    body = (await _fetch(client, world)).json()
    assert [a["name"] for a in body["attendees"]] == ["Dev", "Bina", "Chirag"]
    assert body["visible"] is False and body["matches"] == 2


async def test_reciprocity_is_one_switch(client, world, session, monkeypatch):
    from app.api.events import networking

    monkeypatch.setattr(networking, "HIDDEN_CAN_BROWSE", False)
    world["me"].networking_visible = False
    await session.commit()
    r = await _fetch(client, world)
    assert r.status_code == 403 and r.json()["error_code"] == "NETWORKING_HIDDEN"


async def test_a_block_either_way_leaves_both_out(client, world, session):
    from app.api.reports.models import UserBlocks

    session.add(UserBlocks(blocker_id=world["me"].id, blocked_id=world["twin"].id))
    await session.commit()
    assert await _names(client, world) == ["Bina", "Chirag"]

    # Bina blocked me: I don't see her either.
    session.add(UserBlocks(blocker_id=world["half"].id, blocked_id=world["me"].id))
    await session.commit()
    assert await _names(client, world) == ["Chirag"]


async def test_the_refusals_carry_codes_the_app_can_word(client, world, session, make_user, make_registration, mint_token):
    from tests.conftest import auth_header as header

    outsider = await make_user(email="late@example.com")
    await make_registration(event=world["event"], user=outsider, is_attended=False)
    await session.commit()
    me = world["h"]
    world["h"] = header(await mint_token(outsider))
    r = await _fetch(client, world)
    assert r.status_code == 403 and r.json()["error_code"] == "NETWORKING_NOT_CHECKED_IN"

    from sqlalchemy import update

    from app.api.events.models import Events

    world["h"] = me
    # The page's read let go of the event: changed by id.
    await session.execute(update(Events).where(Events.id == world["event"].id).values(event_datetime=NOW - timedelta(hours=5)))
    await session.commit()
    r = await _fetch(client, world)
    assert r.status_code == 403 and r.json()["error_code"] == "NETWORKING_CLOSED"


# ─── The switch, through the API ──────────────────────────────────────────


async def test_the_switch_is_on_unless_turned_off_and_round_trips(client, world):
    me = await client.get("/api/v1/user/profile/me", headers=world["h"])
    assert me.status_code == 200 and me.json()["networking_visible"] is True

    off = await client.patch("/api/v1/user/preferences", json={"networking_visible": False}, headers=world["h"])
    assert off.status_code == 200 and off.json() == {"networking_visible": False}
    assert (await client.get("/api/v1/user/profile/me", headers=world["h"])).json()["networking_visible"] is False
    assert (await _fetch(client, world)).json()["visible"] is False

    # Nothing sent, nothing changed.
    same = await client.patch("/api/v1/user/preferences", json={}, headers=world["h"])
    assert same.json() == {"networking_visible": False}
    on = await client.patch("/api/v1/user/preferences", json={"networking_visible": True}, headers=world["h"])
    assert on.json() == {"networking_visible": True}


async def test_the_switch_needs_an_account(client):
    r = await client.patch("/api/v1/user/preferences", json={"networking_visible": False})
    assert r.status_code in (401, 403)


async def test_people_already_here_show_after_the_migration(session):
    """The column's default is what the migration gives every existing row."""
    from sqlalchemy import text

    await session.execute(text(
        "INSERT INTO users (full_name, username, email, user_type, token_version, is_active, notify_new_device, admin_role, "
        "totp_enabled, is_deleted, created_at, updated_at) VALUES ('Old Timer', 'oldtimer', 'old@example.com', 'app_user', 0, "
        "true, true, 'owner', false, false, now(), now())"
    ))
    shown = await session.scalar(text("SELECT networking_visible FROM users WHERE username = 'oldtimer'"))
    assert shown is True


# ─── The event page: whether People Like You is on ────────────────────────


async def test_the_event_says_whether_networking_is_on_to_someone_registered(client, world, session, make_user, mint_token):
    world["session"].expunge_all()
    r = await client.get(f"/api/v1/events/info/{world['event'].id}", headers=world["h"])
    assert r.status_code == 200 and r.json()["networking"] is True

    stranger = await make_user(email="stranger@example.com")
    await session.commit()
    r = await client.get(f"/api/v1/events/info/{world['event'].id}", headers=auth_header(await mint_token(stranger)))
    assert r.json()["networking"] is None
