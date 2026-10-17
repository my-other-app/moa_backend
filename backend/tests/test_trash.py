"""The admin's Trash: every way a club is deleted lands it there, and restore
brings it back.

Why it was empty in production: every request's session hides soft-deleted
rows from every ORM query (app.db.listeners), so the Trash's own
`is_deleted == True` was ANDed with `is_deleted IS false` and found nothing,
and restore 404'd. The suite's session did not install that listener, so the
tests passed. It does now (tests/conftest.py), and the Trash and restore say
`include_deleted=True`.
"""
from tests.conftest import auth_header

API = "/api/v1"


async def _header(make_admin, mint_token, email="trash-admin@example.com"):
    return auth_header(await mint_token(await make_admin(email=email)))


async def _trash(client, header, **query):
    response = await client.get(f"{API}/clubs/admin/deleted/list", params=query, headers=header)
    assert response.status_code == 200, response.text
    return response.json()


async def _org(session, name="Trash University"):
    from app.api.orgs.models import Organizations, OrgTypes

    org = Organizations(name=name, type=OrgTypes.college)
    session.add(org)
    await session.flush()
    return org


async def test_the_session_hides_deleted_rows_as_production_does(session, make_club):
    """The premise: without this the rest of the file proves nothing."""
    from sqlalchemy import select

    from app.api.clubs.models import Clubs

    _, club = await make_club(name="Hidden", email="hidden@example.com")
    club.soft_delete()
    await session.commit()
    assert await session.scalar(select(Clubs).where(Clubs.id == club.id)) is None
    seen = await session.scalar(select(Clubs).where(Clubs.id == club.id).execution_options(include_deleted=True))
    assert seen is not None and seen.is_deleted is True


async def test_an_admin_delete_lands_in_the_trash_and_restores(client, session, make_admin, make_club, mint_token):
    header = await _header(make_admin, mint_token)
    owner, club = await make_club(name="Org Club", email="orgclub@example.com")
    org = await _org(session)
    club.org_id = org.id
    await session.commit()

    deleted = await client.delete(f"{API}/clubs/admin/{club.id}", headers=header)
    # A club in an organisation: the answer read `org` lazily and 500'd
    # after the delete had committed.
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["org"] == {"id": org.id, "name": "Trash University"}

    trash = await _trash(client, header)
    assert trash["total"] == 1
    [row] = trash["items"]
    assert (row["id"], row["name"], row["slug"], row["is_verified"]) == (club.id, "Org Club", club.slug, True)
    assert row["deleted_at"] is not None and row["created_at"] is not None
    assert row["org"] == {"id": org.id, "name": "Trash University"}
    assert row["owner_email"] == "orgclub@example.com"

    restored = await client.post(f"{API}/clubs/admin/{club.id}/restore", headers=header)
    assert restored.status_code == 200, restored.text
    assert restored.json()["org"]["id"] == org.id
    assert (await _trash(client, header))["total"] == 0
    listed = await client.get(f"{API}/clubs/admin/list", params={"search": "Org Club"}, headers=header)
    assert [c["id"] for c in listed.json()["items"]] == [club.id]


async def test_a_rejected_application_lands_in_the_trash(client, session, make_admin, make_club, mint_token):
    header = await _header(make_admin, mint_token)
    _, club = await make_club(name="Rejected Applicant", email="rejected@example.com", is_verified=False)

    rejected = await client.post(f"{API}/clubs/{club.id}/reject", headers=header)
    assert rejected.status_code == 200, rejected.text
    [row] = (await _trash(client, header))["items"]
    assert (row["id"], row["is_verified"]) == (club.id, False)

    assert (await client.post(f"{API}/clubs/admin/{club.id}/restore", headers=header)).status_code == 200
    assert (await _trash(client, header))["total"] == 0


async def test_deleting_a_clubs_account_puts_the_club_in_the_trash(client, session, make_admin, make_club, mint_token):
    """The nearest thing to a club deleting itself: its account going. The
    club stayed live and unusable, and never reached the Trash."""
    from sqlalchemy import select

    from app.api.users.models import Users

    header = await _header(make_admin, mint_token)
    owner, club = await make_club(name="Accountless", email="accountless@example.com")

    gone = await client.delete(f"{API}/user/admin/{owner.id}", headers=header)
    assert gone.status_code == 200, gone.text
    [row] = (await _trash(client, header))["items"]
    assert row["id"] == club.id and row["owner_email"] == "accountless@example.com"

    assert (await client.post(f"{API}/clubs/admin/{club.id}/restore", headers=header)).status_code == 200
    account = await session.scalar(
        select(Users).where(Users.id == owner.id).execution_options(include_deleted=True, populate_existing=True)
    )
    assert account.is_deleted is False and account.deleted_at is None
    # The owner can be found again the ordinary way.
    assert await session.scalar(select(Users.id).where(Users.id == owner.id)) == owner.id


async def test_deleting_an_app_users_account_touches_no_club(client, session, make_admin, make_user, make_club, mint_token):
    header = await _header(make_admin, mint_token)
    user = await make_user(email="plainuser@example.com")
    await make_club(name="Bystander", email="bystander@example.com")

    assert (await client.delete(f"{API}/user/admin/{user.id}", headers=header)).status_code == 200
    assert (await _trash(client, header))["total"] == 0


async def test_an_organisation_delete_leaves_its_clubs_live(client, session, make_admin, make_club, mint_token):
    """Deleting an organisation does not delete its clubs: they are not in
    the clubs' Trash because they are not deleted. The organisation is in
    its own."""
    header = await _header(make_admin, mint_token)
    org = await _org(session, "Closing College")
    _, club = await make_club(name="Orphaned", email="orphaned@example.com")
    club.org_id = org.id
    await session.commit()

    assert (await client.delete(f"{API}/orgs/delete/{org.id}", headers=header)).status_code == 200
    assert (await _trash(client, header))["total"] == 0
    detail = await client.get(f"{API}/clubs/admin/{club.id}", headers=header)
    assert detail.status_code == 200 and detail.json()["is_deleted"] is False

    orgs = await client.get(f"{API}/orgs/deleted/list", headers=header)
    assert orgs.status_code == 200, orgs.text
    assert [o["id"] for o in orgs.json()["items"]] == [org.id]
    assert (await client.post(f"{API}/orgs/{org.id}/restore", headers=header)).status_code == 200
    assert (await client.get(f"{API}/orgs/deleted/list", headers=header)).json()["total"] == 0


async def test_a_deleted_event_is_in_its_trash_and_restores(client, session, make_admin, make_club, make_event, mint_token):
    header = await _header(make_admin, mint_token)
    admin_user, club = await make_club(name="Event Club", email="eventclub@example.com")
    event = await make_event(club=club, name="Binned Event", created_by=admin_user)
    event.soft_delete()
    await session.commit()

    events = await client.get(f"{API}/events/admin/deleted/list", headers=header)
    assert events.status_code == 200, events.text
    assert [e["id"] for e in events.json()["items"]] == [event.id]
    assert (await client.post(f"{API}/events/admin/{event.id}/restore", headers=header)).status_code == 200
    assert (await client.get(f"{API}/events/admin/deleted/list", headers=header)).json()["total"] == 0


async def test_the_trash_pages(client, session, make_admin, make_club, mint_token):
    header = await _header(make_admin, mint_token)
    ids = []
    for i in range(3):
        _, club = await make_club(name=f"Binned {i}", email=f"binned{i}@example.com")
        ids.append(club.id)
        assert (await client.delete(f"{API}/clubs/admin/{club.id}", headers=header)).status_code == 200
    await make_club(name="Kept", email="kept@example.com")

    first = await _trash(client, header, limit=2, offset=0)
    second = await _trash(client, header, limit=2, offset=2)
    assert first["total"] == second["total"] == 3
    assert first["next"] is not None and second["next"] is None
    # Newest deletion first, every one exactly once, no live club.
    assert [c["id"] for c in first["items"] + second["items"]] == list(reversed(ids))


async def test_restoring_twice_is_a_404_not_a_second_restore(client, session, make_admin, make_club, mint_token):
    header = await _header(make_admin, mint_token)
    _, club = await make_club(name="Once", email="once@example.com")
    await client.delete(f"{API}/clubs/admin/{club.id}", headers=header)
    assert (await client.post(f"{API}/clubs/admin/{club.id}/restore", headers=header)).status_code == 200
    assert (await client.post(f"{API}/clubs/admin/{club.id}/restore", headers=header)).status_code == 404
