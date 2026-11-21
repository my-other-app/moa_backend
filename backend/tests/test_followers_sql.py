"""The followers page in SQL gives what the Python version gave
(tests/followers_reference.py, the code before): every segment, sort,
search and page of the list, the counts above it, the audience insights and
the export, on a club whose followers cover the cases -- no profile, an
empty name, a deleted organisation, avatar or link, people who came, never
came or have something ahead, follows made in the same instant, names in
mixed case and with accents, a deleted account, a follow undone or deleted,
registrations deleted or for another club's event, more interests than are
shown, one interest deleted and one pick taken back.

And the page's statements do not grow with the club's followers: no id of
theirs goes back to the database as a parameter."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from sqlalchemy import update

from app.api.clubs import followers_insights as followers
from app.api.clubs.models import ClubUsersLink
from app.api.interests.models import InterestCategory, Interests
from app.api.links.models import TrackingLinks
from app.api.orgs.models import OrgTypes, Organizations
from app.api.users.models import UserAvatars, UserInterests, UserProfiles, Users
from tests import followers_reference as reference
from tests.racing import statements

NOW = datetime.now(timezone.utc)

NAMES = [
    "Ananya Iyer", "rohan patil", "Émile Zola", "Zoë Saldana", "ab", "a b", "Bob", "alice", "Ángel Ruiz", "", "Someone Else",
    "Ravi Kumar", "ravi kumar", "Priya", "priya", "Old Timer", "Fresh Face", "Quiet One", "Mid Way", "Ünal", "Ölmez", "Zed",
    "Yusuf", "Xavier", "Wren",
]


@pytest.fixture
async def club_world(session, make_club, make_event, make_user, make_registration):
    owner, club = await make_club(name="Equal Club", email="equal-owner@example.com")
    _, rival = await make_club(name="Rival Club", email="equal-rival@example.com")
    past = [await make_event(club=club, created_by=owner, name=f"Past {i}", event_datetime=NOW - timedelta(days=20 + 40 * i), duration=2.0) for i in range(3)]
    ahead = await make_event(club=club, created_by=owner, name="Ahead", event_datetime=NOW + timedelta(days=4), duration=2.0)
    gone = await make_event(club=club, created_by=owner, name="Gone", event_datetime=NOW - timedelta(days=5), duration=2.0)
    elsewhere = await make_event(club=rival, created_by=owner, name="Elsewhere", event_datetime=NOW - timedelta(days=3), duration=2.0)

    orgs = [Organizations(name=n, type=OrgTypes.college) for n in ("IIT Madras", "NIT Calicut", "Gone College", "")]
    category = InterestCategory(name="Equal topic", slug="equal-topic")
    session.add_all([*orgs, category])
    await session.flush()
    interests = [Interests(name=f"Equal interest {i}", slug=f"equal-interest-{i}", category_id=category.id) for i in range(7)]
    avatars = [UserAvatars(name="Fox", image="avatars/fox.png"), UserAvatars(name="Gone", image="avatars/gone.png")]
    links = [
        TrackingLinks(club_id=club.id, name=n, code=f"eq{i}code", utm_source="x", utm_medium="y")
        for i, n in enumerate(("Instagram bio", "Freshers fair QR", "Deleted link", ""))
    ]
    session.add_all([*interests, *avatars, *links])
    await session.flush()
    orgs[2].is_deleted = True
    interests[6].is_deleted = True
    avatars[1].is_deleted = True
    links[2].is_deleted = True

    people = []
    for i, name in enumerate(NAMES):
        user = await make_user(email=f"equal{i}@example.com")
        user.full_name = name if i % 5 != 1 else (name or "x")
        people.append(user)
        if i % 4 != 3:  # every fourth has no profile
            session.add(UserProfiles(
                user_id=user.id, full_name=(name if i % 3 else ""), org_id=orgs[i % 4].id if i % 6 != 5 else None,
                avatar_id=avatars[i % 2].id if i % 7 in (2, 3) else None,
                profile_pic=f"users/profile_pic/eq{i}.jpg" if i % 7 == 2 else None,
            ))
        for k in range(i % 7):
            # One pick taken back (soft-deleted) for every fifth person.
            session.add(UserInterests(user_id=user.id, interest_id=interests[(i + k) % 7].id, is_deleted=(k == 1 and i % 5 == 3)))
    await session.flush()

    # Follows: some in the same instant, some new, some quiet; a deleted
    # account, a follow undone and a follow deleted are not followers.
    for i, user in enumerate(people):
        followed = NOW - timedelta(days=(i * 13) % 400 + 1, hours=(0 if i % 6 == 0 else i))
        session.add(ClubUsersLink(
            club_id=club.id, user_id=user.id, is_following=True, created_at=followed,
            tracking_link_id=links[i % 4].id if i % 3 == 0 else None,
        ))
    ghost = await make_user(email="equal-ghost@example.com")
    undone = await make_user(email="equal-undone@example.com")
    deleted = await make_user(email="equal-deleted@example.com")
    session.add_all([
        ClubUsersLink(club_id=club.id, user_id=ghost.id, is_following=True, created_at=NOW - timedelta(days=2)),
        ClubUsersLink(club_id=club.id, user_id=undone.id, is_following=False, created_at=NOW - timedelta(days=2)),
        ClubUsersLink(club_id=club.id, user_id=deleted.id, is_following=True, is_deleted=True, created_at=NOW - timedelta(days=2)),
    ])
    ghost.is_deleted = True

    # What they did: came to 0-3 of the club's events, something ahead, a
    # deleted registration, an event deleted since, another club's event.
    for i, user in enumerate(people):
        for j in range(i % 4):
            await make_registration(event=past[j], user=user, is_attended=(j < i % 3))
        if i % 5 == 0:
            await make_registration(event=ahead, user=user)
        if i % 6 == 1:
            await make_registration(event=gone, user=user, is_attended=True)
        if i % 7 == 4:
            await make_registration(event=elsewhere, user=user, is_attended=True)
        if i % 8 == 2:
            await make_registration(event=ahead, user=user, is_deleted=True)
    await session.flush()
    await session.execute(update(type(gone)).where(type(gone).id == gone.id).values(is_deleted=True))
    await session.commit()
    return club


def _plain(rows):
    """Rows as compared: the picture by its key (its links are signed afresh on each read)."""
    out = []
    for r in rows:
        r = dict(r)
        user = dict(r["user"])
        if user["avatar"] is not None:
            user["avatar"] = urlsplit(user["avatar"]["original"]).path
        r["user"] = user
        out.append(r)
    return out


async def test_every_list_page_is_the_one_the_python_version_gave(session, club_world):
    club_id = club_world.id
    ran = 0
    for segment in followers.SEGMENTS:
        for sort in followers.SORTS:
            for search in (None, "", "  ", "a", "RAVI", "someone", "iit", "é", "zz-nothing"):
                for limit, offset in ((24, 0), (5, 0), (5, 5), (7, 21)):
                    want = await reference.list_followers(session, club_id, segment=segment, search=search, sort=sort, limit=limit, offset=offset)
                    got = await followers.list_followers(session, club_id, segment=segment, search=search, sort=sort, limit=limit, offset=offset)
                    context = (segment, sort, search, limit, offset)
                    assert got[1:] == want[1:], context
                    assert _plain(got[0]) == _plain(want[0]), context
                    ran += 1
    assert ran == len(followers.SEGMENTS) * len(followers.SORTS) * 9 * 4
    # The world does cover the cases.
    _, total, counts = await followers.list_followers(session, club_id)
    assert total == len(NAMES) and all(counts[s] for s in followers.SEGMENTS)


async def test_the_audience_above_the_list_is_the_one_the_python_version_gave(session, club_world):
    want = await reference.insights(session, club_world.id)
    got = await followers.insights(session, club_world.id)
    for key in set(want) - {"generated_at"}:
        assert got[key] == want[key], key
    assert got["interests"] and got["by_org"] and got["sources"]


async def test_the_export_is_the_one_the_python_version_gave(session, club_world):
    for segment in followers.SEGMENTS:
        rows, _, _ = await reference.list_followers(session, club_world.id, segment=segment, limit=100_000, offset=0)
        want = [
            [r["user"]["full_name"], r["user"]["username"] or "", r["org"] or "", r["followed_at"].date(), r["registrations"],
             r["attended"], r["last_registered_at"].date() if r["last_registered_at"] else "", "; ".join(r["interests"]), r["source"] or ""]
            for r in rows
        ]
        assert list(await followers.export_rows(session, club_world.id, segment=segment)) == want, segment


async def test_a_page_reads_the_same_statements_however_many_follow(session, make_club, make_user):
    owner, club = await make_club(name="Count Club", email="count-owner@example.com")

    async def follow(n, start):
        for i in range(start, start + n):
            user = await make_user(email=f"count{i}@example.com")
            session.add(ClubUsersLink(club_id=club.id, user_id=user.id, is_following=True, created_at=NOW - timedelta(minutes=i)))
        await session.flush()

    async def page():
        with statements(session) as seen:
            await followers.list_followers(session, club.id, limit=24)
            await followers.insights(session, club.id)
        return [s for s in seen if s.lstrip().upper().startswith("SELECT")]

    await follow(5, 0)
    few = await page()
    await follow(60, 5)
    many = await page()
    assert len(few) == len(many)
    # No follower id comes back as a parameter: the statements are the same text.
    assert few == many
