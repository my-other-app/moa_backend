"""Covers the club-visibility fix and the newly added approve/reject endpoints."""
from tests.conftest import auth_header


async def _club_ids_in(response):
    return {item["id"] for item in response.json()["items"]}


async def test_unverified_club_hidden_from_public_list_but_visible_to_admin(
    client, make_club, make_user, make_admin, mint_token
):
    _, pending_club = await make_club(name="Pending Club", email="pending1@example.com", is_verified=False)
    viewer = await make_user(email="viewer1@example.com")
    admin = await make_admin(email="admin1@example.com")

    public_list = await client.get(
        "/api/v1/clubs/list", headers=auth_header(await mint_token(viewer))
    )
    assert public_list.status_code == 200
    assert pending_club.id not in await _club_ids_in(public_list)

    admin_list = await client.get(
        "/api/v1/clubs/admin/list", headers=auth_header(await mint_token(admin))
    )
    assert admin_list.status_code == 200
    assert pending_club.id in await _club_ids_in(admin_list)


async def test_approve_makes_club_public_and_able_to_log_in(
    client, make_club, make_user, make_admin, mint_token
):
    club_user, pending_club = await make_club(
        name="Approve Me", email="approve1@example.com", is_verified=False
    )
    viewer = await make_user(email="viewer2@example.com")
    admin = await make_admin(email="admin2@example.com")

    approve = await client.post(
        f"/api/v1/clubs/{pending_club.id}/approve", headers=auth_header(await mint_token(admin))
    )
    assert approve.status_code == 200

    public_list = await client.get(
        "/api/v1/clubs/list", headers=auth_header(await mint_token(viewer))
    )
    assert pending_club.id in await _club_ids_in(public_list)

    login = await client.post(
        "/api/v1/auth/token",
        data={"username": club_user.username, "password": "Password123!"},
    )
    assert login.status_code == 200


async def test_reject_soft_deletes_club(client, make_club, make_admin, mint_token):
    club_user, pending_club = await make_club(
        name="Reject Me", email="reject1@example.com", is_verified=False
    )
    admin = await make_admin(email="admin3@example.com")

    reject = await client.post(
        f"/api/v1/clubs/{pending_club.id}/reject", headers=auth_header(await mint_token(admin))
    )
    assert reject.status_code == 200

    admin_list = await client.get(
        "/api/v1/clubs/admin/list", headers=auth_header(await mint_token(admin))
    )
    assert pending_club.id not in await _club_ids_in(admin_list)

    login = await client.post(
        "/api/v1/auth/token",
        data={"username": club_user.username, "password": "Password123!"},
    )
    assert login.status_code == 401
