"""The per-row fields the v2 Clubs table shows.

The admin list returned a club's name, logo and verification flag and nothing
else, so the table could not show the organisation it belongs to, how many
events it has run, or the address it signs in with -- each of which was a
click into the club to find out.

They are added behind `with_admin_fields`, off by default, because
`get_all_clubs` also answers the public club listing, which shows none of
them and is the hot path.
"""

import pytest

from tests.conftest import auth_header


def row_for(response, club_id):
    for item in response.json()["items"]:
        if item["id"] == club_id:
            return item
    raise AssertionError(f"club {club_id} not in {[i['id'] for i in response.json()['items']]}")


@pytest.mark.asyncio
async def test_list_reports_the_organisation(
    client, session, make_admin, make_club, mint_token
):
    from app.api.orgs.models import Organizations, OrgTypes

    admin = await make_admin()
    org = Organizations(name="COEP Technological University", type=OrgTypes.college)
    session.add(org)
    await session.flush()
    _, club = await make_club(name="Circuit Society", email="circuit@example.com")
    club.org_id = org.id
    await session.flush()
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert row_for(response, club.id)["org"] == {"id": org.id, "name": org.name}


@pytest.mark.asyncio
async def test_list_reports_no_organisation_as_null(
    client, make_admin, make_club, mint_token
):
    """Most clubs have none, and the column has to render for them too."""
    admin = await make_admin()
    _, club = await make_club(name="Lone Club", email="lone@example.com")
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert row_for(response, club.id)["org"] is None


@pytest.mark.asyncio
async def test_list_reports_the_login_email(client, make_admin, make_club, mint_token):
    """
    The sign-in address, not contact_email.

    contact_email is published on the club's page and is usually a committee
    alias; the one an admin needs when resetting a password is the account's.
    """
    admin = await make_admin()
    user, club = await make_club(name="Quiz Club", email="quiz.login@example.com")
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert row_for(response, club.id)["owner_email"] == user.email


@pytest.mark.asyncio
async def test_list_counts_events_per_club(
    client, make_admin, make_club, make_event, mint_token
):
    admin = await make_admin()
    user, busy = await make_club(name="Busy Club", email="busy@example.com")
    _, quiet = await make_club(name="Quiet Club", email="quiet@example.com")
    for i in range(3):
        await make_event(club=busy, name=f"Event {i}", created_by=user)
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert row_for(response, busy.id)["events_count"] == 3
    assert row_for(response, quiet.id)["events_count"] == 0


@pytest.mark.asyncio
async def test_the_event_count_does_not_inflate_the_follower_count(
    client, session, make_admin, make_club, make_event, make_user, mint_token
):
    """
    The one that would go unnoticed.

    The listing already carries an outer join to the follower link table. A
    second aggregate over a second join multiplies the rows each one counts,
    so a club with 3 events and 2 followers reports 6 followers -- a plausible
    number, on a screen full of plausible numbers. The counts are taken from a
    separate grouped query for exactly this reason.
    """
    from app.api.clubs.models import ClubUsersLink

    admin = await make_admin()
    user, club = await make_club(name="Both Club", email="both@example.com")
    for i in range(3):
        await make_event(club=club, name=f"Event {i}", created_by=user)
    for i in range(2):
        follower = await make_user(email=f"follower{i}@example.com")
        session.add(
            ClubUsersLink(club_id=club.id, user_id=follower.id, is_following=True)
        )
    await session.flush()
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    row = row_for(response, club.id)
    assert row["events_count"] == 3
    assert row["followers_count"] == 2


@pytest.mark.asyncio
async def test_approving_a_club_still_serialises(
    client, make_admin, make_club, mint_token
):
    """
    approve/reject return the same schema, built without the extra fields.

    Making any of them required would turn an approval into a 500 raised
    after the write had already committed -- the club would be approved and
    the dashboard would report that it had failed.
    """
    admin = await make_admin()
    _, club = await make_club(name="Pending Club", email="pending@example.com", is_verified=False)
    token = await mint_token(admin)

    response = await client.post(
        f"/api/v1/clubs/{club.id}/approve", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_verified"] is True
    assert body["events_count"] == 0
    assert body["org"] is None
