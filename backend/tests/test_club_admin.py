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


@pytest.mark.asyncio
async def test_a_deleted_club_can_be_found_and_restored(
    client, make_admin, make_club, mint_token
):
    """
    Everything soft-deletes and nothing restored, so reject and delete were
    permanent in practice -- the confirm dialogs said a developer could undo
    it, which was true and also an admission the product could not.
    """
    admin = await make_admin()
    _, club = await make_club(name="Oops", email="oops@example.com")
    token = await mint_token(admin)

    await client.delete(f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token))

    deleted = await client.get(
        "/api/v1/clubs/admin/deleted/list", headers=auth_header(token)
    )
    assert deleted.status_code == 200, deleted.text
    assert [c["name"] for c in deleted.json()["items"]] == ["Oops"]

    restored = await client.post(
        f"/api/v1/clubs/admin/{club.id}/restore", headers=auth_header(token)
    )
    assert restored.status_code == 200, restored.text

    # Back in the normal listing, and out of the deleted one.
    live = await client.get(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )
    assert live.status_code == 200
    assert live.json()["is_deleted"] is False

    still_deleted = await client.get(
        "/api/v1/clubs/admin/deleted/list", headers=auth_header(token)
    )
    assert still_deleted.json()["total"] == 0


@pytest.mark.asyncio
async def test_restoring_does_not_re_verify_a_pending_club(
    client, make_admin, make_club, mint_token
):
    """An undo must not silently make the approval decision for the admin."""
    admin = await make_admin()
    _, club = await make_club(
        name="Pending Undo", email="pendingundo@example.com", is_verified=False
    )
    token = await mint_token(admin)

    await client.post(f"/api/v1/clubs/{club.id}/reject", headers=auth_header(token))
    await client.post(
        f"/api/v1/clubs/admin/{club.id}/restore", headers=auth_header(token)
    )

    detail = await client.get(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )
    assert detail.json()["is_verified"] is False


@pytest.mark.asyncio
async def test_restoring_a_live_club_is_a_404(
    client, make_admin, make_club, mint_token
):
    admin = await make_admin()
    _, club = await make_club(name="Alive", email="alive@example.com")
    token = await mint_token(admin)

    response = await client.post(
        f"/api/v1/clubs/admin/{club.id}/restore", headers=auth_header(token)
    )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_restore_is_recorded(client, make_admin, make_club, mint_token):
    admin = await make_admin()
    _, club = await make_club(name="Logged Undo", email="loggedundo@example.com")
    token = await mint_token(admin)

    await client.delete(f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token))
    await client.post(
        f"/api/v1/clubs/admin/{club.id}/restore", headers=auth_header(token)
    )

    log = await client.get(
        "/api/v1/admin/audit-log?action=club.restore", headers=auth_header(token)
    )
    assert log.json()["total"] == 1


@pytest.mark.asyncio
async def test_the_deleted_list_is_admin_only(client, make_user, mint_token):
    nosy = await make_user(email="nosydeleted@example.com")
    token = await mint_token(nosy)

    response = await client.get(
        "/api/v1/clubs/admin/deleted/list", headers=auth_header(token)
    )

    assert response.status_code in (401, 403), response.text
