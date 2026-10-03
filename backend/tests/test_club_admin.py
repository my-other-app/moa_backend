"""Tests for the admin club endpoints.

The dashboard could list clubs and approve or reject them, but could not open
one. Worse, the only detail endpoint -- /clubs/info/{id} -- enforces
`is_verified`, so a pending club 404s: the admin was asked to approve or
reject something they could not look at.
"""

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_admin_can_open_a_pending_club(
    client, make_admin, make_club, mint_token
):
    """The whole point: the public endpoint 404s on an unverified club."""
    admin = await make_admin()
    _, club = await make_club(
        name="Pending Club", email="pendingclub@example.com", is_verified=False
    )
    token = await mint_token(admin)

    public = await client.get(
        f"/api/v1/clubs/info/{club.id}", headers=auth_header(token)
    )
    assert public.status_code == 404

    admin_view = await client.get(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )
    assert admin_view.status_code == 200, admin_view.text
    body = admin_view.json()
    assert body["name"] == "Pending Club"
    assert body["is_verified"] is False


@pytest.mark.asyncio
async def test_detail_reports_reach_and_revenue_across_events(
    client, make_admin, make_club, make_user, make_event, make_registration, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="Money Club", email="moneyclub@example.com")

    one = await make_event(club=club, name="One", created_by=owner)
    two = await make_event(club=club, name="Two", created_by=owner)
    a = await make_user(email="a@example.com")
    b = await make_user(email="b@example.com")
    c = await make_user(email="c@example.com")
    await make_registration(event=one, user=a, is_paid=True, paid_amount=100)
    await make_registration(event=one, user=b, is_attended=True)
    await make_registration(event=two, user=c, is_paid=True, paid_amount=150,
                            is_attended=True)

    token = await mint_token(admin)
    response = await client.get(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    stats = response.json()["admin_stats"]
    assert stats["total_registrations"] == 3
    assert stats["total_attended"] == 2
    assert stats["total_revenue"] == 250


@pytest.mark.asyncio
async def test_detail_names_the_account_owner(
    client, make_admin, make_club, mint_token
):
    admin = await make_admin()
    _, club = await make_club(name="Owned Club", email="ownedclub@example.com")
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["owner"]["email"] == "ownedclub@example.com"


@pytest.mark.asyncio
async def test_admin_can_edit_any_club(client, make_admin, make_club, mint_token):
    """PUT /clubs/update only ever edits the caller's own club."""
    admin = await make_admin()
    _, club = await make_club(name="Before", email="editable@example.com")
    token = await mint_token(admin)

    response = await client.put(
        f"/api/v1/clubs/admin/{club.id}",
        headers=auth_header(token),
        data={"name": "After", "location_name": "Kochi"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "After"

    check = await client.get(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )
    assert check.json()["location_name"] == "Kochi"


@pytest.mark.asyncio
async def test_admin_can_delete_a_club(client, make_admin, make_club, mint_token):
    admin = await make_admin()
    _, club = await make_club(name="Doomed", email="doomed@example.com")
    token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )
    assert response.status_code == 200, response.text

    # Soft-deleted: gone from the public endpoint.
    public = await client.get(
        f"/api/v1/clubs/info/{club.id}", headers=auth_header(token)
    )
    assert public.status_code == 404


@pytest.mark.asyncio
async def test_admin_list_is_not_shadowed_by_the_detail_route(
    client, make_admin, mint_token
):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert "items" in response.json()


@pytest.mark.asyncio
async def test_a_club_cannot_read_another_club(
    client, make_club, mint_token
):
    owner, _ = await make_club(name="Nosy", email="nosyclub@example.com")
    _, other = await make_club(name="Other", email="otherclub@example.com")
    token = await mint_token(owner)

    response = await client.get(
        f"/api/v1/clubs/admin/{other.id}", headers=auth_header(token)
    )

    assert response.status_code in (401, 403), response.text


@pytest.mark.asyncio
async def test_missing_club_is_a_404(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/clubs/admin/999999", headers=auth_header(token))

    assert response.status_code == 404, response.text
