"""A person's picture wherever the admin portal (and a club's door team and
reviews) shows them: the photo they uploaded, else the preset avatar they
picked -- the dashboards fall back to initials only when there is neither.

The users list and one user (GET /user/admin/list, /user/admin/{id}) already
sent both under `profile`; the portal dropped them on its side. These hold
that the server keeps sending them, and that reading them for a page costs a
fixed number of queries however many people are on it, never one per row.

The audit log's actor, the reports queue's people, a badge's recipients and
a door team's volunteers carried no picture at all; each now has `avatar`,
resolved as everywhere else (reports.service.avatar). An event review's
reviewer was sent the avatar but never the photo, which wins over it.

Nothing reaches S3: a picture is stored as its column stores a key, and its
links are signed locally.
"""
import pytest
from sqlalchemy import event as sa_event

from app.api.users.models import UserAvatars, UserProfiles
from app.core.notifications import service as push
from tests.conftest import auth_header

API = "/api/v1"
PHOTO = UserProfiles.__table__.c.profile_pic.type
AVATAR = UserAvatars.__table__.c.image.type


def _key(field, name: str) -> str:
    """A key as the column stores it: `name` under the column's folder."""
    return f"{field.base_path.rstrip('/')}/{name}"


def _name(char: str) -> str:
    return f"v-{char * 32}.png"


def _thumb(char: str) -> str:
    """What a thumbnail link of the picture named by `char` contains."""
    return f"/v-{char * 32}.thumbnail.png"


@pytest.fixture
def statements(session):
    """Every statement the test's connection runs, in order."""
    seen: list[str] = []
    conn = session.bind.sync_connection

    def before(_conn, _cursor, statement, *_args):
        seen.append(statement)

    sa_event.listen(conn, "before_cursor_execute", before)
    yield seen
    sa_event.remove(conn, "before_cursor_execute", before)


@pytest.fixture
async def fox(session):
    """A preset avatar to pick (its picture is the "a" one)."""
    row = UserAvatars(name="Fox", image=_key(AVATAR, _name("a")))
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
def person(session, make_user, fox):
    """Someone with a profile and the pictures asked for: a photo (named by
    a hex digit), the fox avatar, both or neither."""
    fox_id = fox.id

    async def _make(email: str, *, name: str, photo: str | None = None, avatar: bool = False, maker=None):
        user = await (maker or make_user)(email=email)
        user.full_name = name
        session.add(
            UserProfiles(
                user_id=user.id,
                full_name=name,
                profile_pic=_key(PHOTO, _name(photo)) if photo else None,
                avatar_id=fox_id if avatar else None,
            )
        )
        await session.flush()
        return user

    return _make


# ─── The users list and one user ────────────────────────────────────────


async def test_the_user_list_carries_each_persons_photo_and_avatar(client, session, make_admin, mint_token, person):
    h = auth_header(await mint_token(await make_admin()))
    await person("both@example.com", name="Both Pictures", photo="b", avatar=True)
    await person("avataronly@example.com", name="Avatar Only", avatar=True)
    await person("nopicture@example.com", name="No Picture")
    await session.commit()
    session.expunge_all()  # read back from the database, as in production

    r = await client.get(f"{API}/user/admin/list", headers=h)
    assert r.status_code == 200, r.text
    rows = {u["username"]: u for u in r.json()["items"]}

    both = rows["both"]["profile"]
    assert _thumb("b") in both["profile_pic"]["thumbnail"] and both["profile_pic"]["original"]
    # The avatar comes too: the photo wins on screen, but both are sent.
    assert both["avatar"]["name"] == "Fox" and _thumb("a") in both["avatar"]["image"]["thumbnail"]

    only = rows["avataronly"]["profile"]
    assert only["profile_pic"] is None and _thumb("a") in only["avatar"]["image"]["thumbnail"]

    none = rows["nopicture"]["profile"]
    assert none["profile_pic"] is None and none["avatar"] is None
    # An account with no profile row at all (the admin here) is a null profile.
    assert rows["admin"]["profile"] is None


async def test_one_user_carries_their_photo_and_avatar(client, session, make_admin, mint_token, person):
    h = auth_header(await mint_token(await make_admin()))
    user = await person("detail@example.com", name="Detail Person", photo="d", avatar=True)
    user_id = user.id
    await session.commit()
    session.expunge_all()

    r = await client.get(f"{API}/user/admin/{user_id}", headers=h)
    assert r.status_code == 200, r.text
    profile = r.json()["profile"]
    assert _thumb("d") in profile["profile_pic"]["thumbnail"]
    assert {"thumbnail", "medium", "large", "original"} <= set(profile["profile_pic"])
    assert _thumb("a") in profile["avatar"]["image"]["thumbnail"]


async def test_the_user_list_reads_pictures_in_the_same_queries_for_any_page_size(
    client, session, make_admin, mint_token, person, statements
):
    """Profiles and avatars come in one query each for the page: six people
    with pictures cost what one does."""
    h = auth_header(await mint_token(await make_admin()))

    async def queries_for_a_page():
        session.expunge_all()
        statements.clear()
        r = await client.get(f"{API}/user/admin/list?limit=50", headers=h)
        assert r.status_code == 200, r.text
        return len(statements), r.json()["items"]

    await person("crowd0@example.com", name="Crowd 0", photo="1", avatar=True)
    await session.commit()
    few, items = await queries_for_a_page()
    assert sum(1 for u in items if u["profile"] and u["profile"]["profile_pic"]) == 1

    for i, digit in enumerate("23456", start=1):
        await person(f"crowd{i}@example.com", name=f"Crowd {i}", photo=digit, avatar=True)
    await session.commit()
    many, items = await queries_for_a_page()
    assert sum(1 for u in items if u["profile"] and u["profile"]["profile_pic"]) == 6
    assert sum(1 for u in items if u["profile"] and u["profile"]["avatar"]) == 6
    assert many == few, statements


# ─── The audit log ──────────────────────────────────────────────────────


async def test_the_audit_log_shows_each_actor_with_their_picture(
    client, session, make_admin, mint_token, person, statements
):
    from app.api.audit import service as audit

    reader = await make_admin(email="reader-audit@example.com")
    h = auth_header(await mint_token(reader))

    async def act(actor, action):
        await audit.record(session, actor=actor, action=action, summary=action)

    photo = await person("photo-audit@example.com", name="Photo Admin", photo="c", maker=make_admin)
    picked = await person("picked-audit@example.com", name="Picked Admin", avatar=True, maker=make_admin)
    await act(photo, "test.photo")
    await act(picked, "test.picked")
    await act(reader, "test.bare")
    await session.commit()

    async def page():
        session.expunge_all()
        statements.clear()
        r = await client.get(f"{API}/admin/audit-log?limit=50", headers=h)
        assert r.status_code == 200, r.text
        return len(statements), {e["action"]: e["actor"] for e in r.json()["items"]}

    few, actors = await page()
    assert _thumb("c") in actors["test.photo"]["avatar"]["thumbnail"]
    assert _thumb("a") in actors["test.picked"]["avatar"]["thumbnail"]
    assert actors["test.bare"]["avatar"] is None
    assert actors["test.photo"]["full_name"] == "Photo Admin" and actors["test.photo"]["email"] == "photo-audit@example.com"

    # More actors, more entries: the same queries.
    for i, digit in enumerate("789", start=1):
        more = await person(f"more{i}-audit@example.com", name=f"More {i}", photo=digit, maker=make_admin)
        await act(more, f"test.more{i}")
    await session.commit()
    many, actors = await page()
    assert all(actors[f"test.more{i}"]["avatar"] for i in (1, 2, 3))
    assert many == few, statements


# ─── The reports queue ──────────────────────────────────────────────────


async def test_the_reports_queue_shows_who_reported_and_who_was_reported_with_pictures(
    client, session, make_admin, mint_token, person, monkeypatch
):
    async def no_push(*args, **kwargs):
        return 0

    monkeypatch.setattr(push, "send_notification_to_users", no_push)
    reporter = await person("reporter-pic@example.com", name="Reporter", photo="e")
    target = await person("target-pic@example.com", name="Target", avatar=True)
    target_id = target.id
    admin_h = auth_header(await mint_token(await make_admin()))
    reporter_h = auth_header(await mint_token(reporter))
    await session.commit()

    made = await client.post(f"{API}/reports", json={"target_type": "user", "target_id": target_id, "reason": "spam"}, headers=reporter_h)
    assert made.status_code in (200, 201), made.text

    session.expunge_all()
    r = await client.get(f"{API}/admin/reports?status=open", headers=admin_h)
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    assert item["reporter"]["name"] == "Reporter" and _thumb("e") in item["reporter"]["avatar"]["thumbnail"]
    assert item["target_author"]["name"] == "Target" and _thumb("a") in item["target_author"]["avatar"]["thumbnail"]


# ─── A badge's recipients ───────────────────────────────────────────────


async def test_a_badges_recipients_carry_their_pictures(client, session, make_admin, mint_token, person):
    from app.api.badges.models import BadgeDefinitions, BadgeType, UserBadges

    badge = BadgeDefinitions(
        slug="pictured", name="Pictured", description="In tests", emoji="🏅",
        badge_type=BadgeType.user, trigger_metric="events_attended", threshold=1,
    )
    session.add(badge)
    await session.flush()
    badge_id = badge.id
    with_photo = await person("holder-photo@example.com", name="Holder Photo", photo="f")
    bare = await person("holder-bare@example.com", name="Holder Bare")
    session.add_all([UserBadges(user_id=with_photo.id, badge_id=badge_id), UserBadges(user_id=bare.id, badge_id=badge_id)])
    h = auth_header(await mint_token(await make_admin()))
    await session.commit()
    session.expunge_all()

    r = await client.get(f"{API}/badges/admin/{badge_id}/recipients", headers=h)
    assert r.status_code == 200, r.text
    by_name = {x["entity_name"]: x for x in r.json()["recipients"]}
    assert _thumb("f") in by_name["Holder Photo"]["avatar"]["thumbnail"]
    assert by_name["Holder Bare"]["avatar"] is None

    stats = await client.get(f"{API}/badges/admin/{badge_id}/stats", headers=h)
    assert stats.status_code == 200, stats.text
    assert any(x["avatar"] for x in stats.json()["recent_awards"])


# ─── A club's door team and an event's reviews ──────────────────────────


async def test_a_door_teams_volunteers_carry_their_pictures(client, session, make_club, make_event, mint_token, person):
    from app.api.events.volunteer.models import Volunteer

    owner, club = await make_club(name="Picture Club", email="picture-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Picture Night")
    event_id = event.id
    helper = await person("helper-pic@example.com", name="Helper", photo="9")
    session.add_all([
        Volunteer(email=helper.email, event_id=event_id, user_id=helper.id, is_approved=True),
        # Invited, not yet an account: no picture to show.
        Volunteer(email="invited-pic@example.com", event_id=event_id, is_approved=False),
    ])
    h = auth_header(await mint_token(owner))
    await session.commit()
    session.expunge_all()

    r = await client.get(f"{API}/events/{event_id}/team", headers=h)
    assert r.status_code == 200, r.text
    by_email = {v["email"]: v for v in r.json()["volunteers"]}
    assert by_email["helper-pic@example.com"]["name"] == "Helper"
    assert _thumb("9") in by_email["helper-pic@example.com"]["avatar"]["thumbnail"]
    assert by_email["invited-pic@example.com"]["avatar"] is None and by_email["invited-pic@example.com"]["name"] is None


async def test_an_events_reviews_send_the_reviewers_photo_beside_the_avatar(
    client, session, make_club, make_event, mint_token, person
):
    from app.api.events.models import EventRatingsLink

    owner, club = await make_club(name="Review Club", email="review-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Review Night")
    event_id = event.id
    reviewer = await person("reviewer-pic@example.com", name="Reviewer", photo="5", avatar=True)
    session.add(EventRatingsLink(event_id=event_id, user_id=reviewer.id, rating=5, review="Loved it"))
    h = auth_header(await mint_token(owner))
    await session.commit()
    session.expunge_all()

    r = await client.get(f"{API}/events/ratings/{event_id}", headers=h)
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    profile = item["user"]["profile"]
    assert _thumb("5") in profile["profile_pic"]["thumbnail"]
    # Still sent as before, for whatever reads it.
    assert _thumb("a") in profile["avatar"]["thumbnail"]
