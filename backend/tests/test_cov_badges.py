"""Badges: a person's, a club's, your own, and a guest's folded into the app
account their address belongs to.

GET /badges/user/{id}, /badges/club/{id}, /badges/me (app/api/badges/router.py),
badges.service.check_and_award_club_badges, users.service._fold_badges.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.badges import service as badges
from app.api.badges.models import BadgeDefinitions, BadgeType, ClubBadges, UserBadges
from app.api.users import service as users
from app.api.users.models import SigninProviders, UserTypes
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def tag():
    """A prefix of this test's own, so definitions any other test or seed
    left behind never count here."""
    return f"cov-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def define(session, tag):
    async def _define(slug, kind=BadgeType.user, metric="events_attended", threshold=1, emoji="*"):
        d = BadgeDefinitions(slug=f"{tag}-{slug}", name=slug.title(), description=f"The {slug} badge", emoji=emoji, badge_type=kind, trigger_metric=metric, threshold=threshold)
        session.add(d)
        await session.flush()
        return d

    return _define


def _mine(body, tag):
    return [b["badge"]["slug"].removeprefix(f"{tag}-") for b in body["badges"] if b["badge"]["slug"].startswith(tag)]


async def test_a_persons_badges_are_theirs_newest_first_and_need_a_sign_in(client, session, make_user, mint_token, define, tag):
    first, star, other = await define("first", emoji="1"), await define("star", emoji="S"), await define("other")
    ana = await make_user(email="ana-badges@example.com")
    viewer = await make_user(email="viewer-badges@example.com")
    session.add_all([
        UserBadges(user_id=ana.id, badge_id=first.id, awarded_at=NOW - timedelta(days=9)),
        UserBadges(user_id=ana.id, badge_id=star.id, awarded_at=NOW - timedelta(days=1)),
        UserBadges(user_id=viewer.id, badge_id=other.id, awarded_at=NOW),
    ])
    await session.commit()

    r = await client.get(f"{API}/badges/user/{ana.id}", headers=auth_header(await mint_token(viewer)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == ana.id
    assert _mine(body, tag) == ["star", "first"]  # newest first, and never the viewer's own
    star_row = body["badges"][0]
    assert star_row["badge"]["emoji"] == "S" and star_row["badge"]["badge_type"] == "user"
    assert star_row["badge"]["trigger_metric"] == "events_attended" and star_row["badge"]["threshold"] == 1
    assert star_row["awarded_at"] is not None

    # Someone with none, or nobody at all: an empty list, not an error.
    nobody = (await client.get(f"{API}/badges/user/987654321", headers=auth_header(await mint_token(viewer)))).json()
    assert nobody == {"user_id": 987654321, "badges": [], "total": 0}

    assert (await client.get(f"{API}/badges/user/{ana.id}")).status_code == 401


async def test_looking_at_someones_badges_awards_them_nothing_but_your_own_page_does(
    client, session, make_club, make_event, make_user, make_registration, mint_token, define, tag
):
    await define("registered", metric="events_registered")
    await define("attended", metric="events_attended")
    await define("regular", metric="events_attended", threshold=5)
    owner, club = await make_club(name="Badge Club", email="badge-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Badge Night", event_datetime=NOW - timedelta(days=2))
    ana = await make_user(email="ana-earned@example.com")
    await make_registration(event=event, user=ana, is_attended=True)
    viewer = await make_user(email="viewer-earned@example.com")
    await session.commit()

    seen = (await client.get(f"{API}/badges/user/{ana.id}", headers=auth_header(await mint_token(viewer)))).json()
    assert _mine(seen, tag) == []
    assert await session.scalar(select(UserBadges).where(UserBadges.user_id == ana.id)) is None

    ana_h = auth_header(await mint_token(ana))
    me = (await client.get(f"{API}/badges/me", headers=ana_h)).json()
    assert me["user_id"] == ana.id
    assert sorted(_mine(me, tag)) == ["attended", "registered"]  # five events is still to come
    # Asking again awards nothing twice.
    again = (await client.get(f"{API}/badges/me", headers=ana_h)).json()
    assert sorted(_mine(again, tag)) == ["attended", "registered"]
    assert len(list(await session.scalars(select(UserBadges).where(UserBadges.user_id == ana.id)))) == me["total"] == again["total"]
    # And now anyone signed in sees them.
    seen = (await client.get(f"{API}/badges/user/{ana.id}", headers=auth_header(await mint_token(viewer)))).json()
    assert sorted(_mine(seen, tag)) == ["attended", "registered"]


async def test_a_clubs_badges_are_public_and_its_own(client, session, make_club, define, tag):
    host, verified = await define("host", kind=BadgeType.club, metric="events_hosted"), await define("verified", kind=BadgeType.club, metric="is_verified")
    _, club = await make_club(name="Public Badges", email="public-badges@example.com")
    _, rival = await make_club(name="Rival Badges", email="rival-badges@example.com")
    session.add_all([
        ClubBadges(club_id=club.id, badge_id=host.id, awarded_at=NOW - timedelta(days=30)),
        ClubBadges(club_id=club.id, badge_id=verified.id, awarded_at=NOW - timedelta(days=2)),
        ClubBadges(club_id=rival.id, badge_id=host.id, awarded_at=NOW),
    ])
    await session.commit()

    r = await client.get(f"{API}/badges/club/{club.id}")  # no sign-in: the club page shows them
    assert r.status_code == 200, r.text
    assert r.json()["club_id"] == club.id
    assert _mine(r.json(), tag) == ["verified", "host"]
    assert r.json()["badges"][0]["badge"]["badge_type"] == "club"
    assert (await client.get(f"{API}/badges/club/987654321")).json() == {"club_id": 987654321, "badges": [], "total": 0}


async def test_a_club_earns_badges_by_hosting_people_followers_and_being_verified(
    client, session, make_club, make_event, make_user, make_registration, define, tag
):
    await define("two-events", kind=BadgeType.club, metric="events_hosted", threshold=2)
    await define("ten-events", kind=BadgeType.club, metric="events_hosted", threshold=10)
    await define("crowd", kind=BadgeType.club, metric="total_attendees", threshold=2)
    await define("verified", kind=BadgeType.club, metric="is_verified", threshold=1)
    await define("followed", kind=BadgeType.club, metric="follower_count", threshold=3)
    await define("loved", kind=BadgeType.club, metric="avg_rating", threshold=4.5)
    owner, club = await make_club(name="Earning Club", email="earning-club@example.com")
    e1 = await make_event(club=club, created_by=owner, name="One", event_datetime=NOW - timedelta(days=5))
    await make_event(club=club, created_by=owner, name="Two", event_datetime=NOW - timedelta(days=4))
    await make_event(club=club, created_by=owner, name="Gone", event_datetime=NOW - timedelta(days=3), is_deleted=True)
    for i, attended in enumerate((True, True, False)):
        u = await make_user(email=f"crowd-{i}@example.com")
        await make_registration(event=e1, user=u, is_attended=attended)
    club.follower_count = 2
    club.rating = 4.6
    await session.commit()

    earned = await badges.check_and_award_club_badges(session, club.id)
    assert sorted(s.removeprefix(f"{tag}-") for s in earned if s.startswith(tag)) == ["crowd", "loved", "two-events", "verified"]
    # Nothing twice; a third follower earns the next one.
    assert [s for s in await badges.check_and_award_club_badges(session, club.id) if s.startswith(tag)] == []
    club.follower_count = 3
    await session.commit()
    assert [s.removeprefix(f"{tag}-") for s in await badges.check_and_award_club_badges(session, club.id) if s.startswith(tag)] == ["followed"]

    shown = (await client.get(f"{API}/badges/club/{club.id}")).json()
    assert sorted(_mine(shown, tag)) == ["crowd", "followed", "loved", "two-events", "verified"]


async def test_an_unverified_club_with_nothing_held_earns_nothing(session, make_club, define, tag):
    await define("host", kind=BadgeType.club, metric="events_hosted", threshold=1)
    await define("verified", kind=BadgeType.club, metric="is_verified", threshold=1)
    _, club = await make_club(name="Quiet Club", email="quiet-club@example.com", is_verified=False)
    await session.commit()
    assert [s for s in await badges.check_and_award_club_badges(session, club.id) if s.startswith(tag)] == []


# --- guests folded into the app account ------------------------------------------


async def test_guest_badges_join_the_account_once_each(client, session, make_user, mint_token, define, tag):
    x, y, z = await define("x"), await define("y"), await define("z")
    app = await make_user(email="Meera@Example.com", username=f"app-{uuid.uuid4().hex[:8]}")
    app.provider = SigninProviders.google
    g1 = await make_user(email="meera@example.com", user_type=UserTypes.guest, username=f"guest-{uuid.uuid4().hex[:8]}")
    g2 = await make_user(email=" MEERA@example.com ", user_type=UserTypes.guest, username=f"guest-{uuid.uuid4().hex[:8]}")
    own_x = UserBadges(user_id=app.id, badge_id=x.id, awarded_at=NOW - timedelta(days=50))
    g1_x = UserBadges(user_id=g1.id, badge_id=x.id, awarded_at=NOW - timedelta(days=40))
    g1_y = UserBadges(user_id=g1.id, badge_id=y.id, awarded_at=NOW - timedelta(days=30))
    g2_y = UserBadges(user_id=g2.id, badge_id=y.id, awarded_at=NOW - timedelta(days=20))
    g2_z = UserBadges(user_id=g2.id, badge_id=z.id, awarded_at=NOW - timedelta(days=10))
    session.add_all([own_x, g1_x, g1_y, g2_y, g2_z])
    await session.flush()

    summary = await users.merge_guest_into_user(session, app)
    await session.commit()
    assert summary["merged"] is True and summary["guests_merged"] == 2
    assert summary["badges_transferred"] == 2  # y (the earlier award) and z; x it had already

    holders = {b.id: b.user_id for b in await session.scalars(select(UserBadges).where(UserBadges.badge_id.in_([x.id, y.id, z.id])))}
    assert holders[own_x.id] == app.id and holders[g1_y.id] == app.id and holders[g2_z.id] == app.id
    # A badge the account already had stays with the retired guest: (user, badge) is unique.
    assert holders[g1_x.id] == g1.id and holders[g2_y.id] == g2.id

    me = (await client.get(f"{API}/badges/me", headers=auth_header(await mint_token(app)))).json()
    assert _mine(me, tag) == ["z", "y", "x"]  # one each, newest first, with their first award dates
    y_row = next(b for b in me["badges"] if b["badge"]["slug"] == f"{tag}-y")
    assert abs((datetime.fromisoformat(y_row["awarded_at"]) - (NOW - timedelta(days=30))).total_seconds()) < 1

    # Folding again moves nothing more.
    assert await users.merge_guest_into_user(session, app) == {"merged": False, "guests_merged": 0}


async def test_an_account_that_has_not_shown_it_owns_the_address_takes_no_guest_badges(session, make_user, define):
    x = await define("x")
    app = await make_user(email="unproven@example.com", username=f"app-{uuid.uuid4().hex[:8]}")  # a password account, never verified
    guest = await make_user(email="unproven@example.com", user_type=UserTypes.guest, username=f"guest-{uuid.uuid4().hex[:8]}")
    badge = UserBadges(user_id=guest.id, badge_id=x.id)
    session.add(badge)
    await session.flush()
    assert (await users.merge_guest_into_user(session, app))["merged"] is False
    await session.refresh(badge)
    assert badge.user_id == guest.id
