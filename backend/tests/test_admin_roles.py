"""Admin roles.

There was one `admin` user_type and nothing else, so anyone who needed to look
at the dashboard could delete clubs, refund payments and impersonate users.
Roles narrow that without a second auth system: `user_type` still gates who is
an admin, `admin_role` gates what kind.

Endpoints declare the capability they need rather than listing roles, so
adding a role later does not mean auditing every route.
"""

import pytest

from app.api.users.models import AdminRoles
from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_an_existing_admin_defaults_to_owner(client, make_admin, mint_token):
    """
    The column is new. Defaulting to viewer would have locked the platform out
    of itself the moment it deployed.
    """
    admin = await make_admin()
    assert admin.admin_role == AdminRoles.owner


@pytest.mark.asyncio
async def test_a_viewer_can_read_but_not_destroy(
    client, make_admin, make_club, mint_token
):
    viewer = await make_admin(email="viewer@example.com", role=AdminRoles.viewer)
    _, club = await make_club(name="Safe", email="safeclub@example.com")
    token = await mint_token(viewer)

    readable = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))
    assert readable.status_code == 200, readable.text

    destructive = await client.delete(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )
    assert destructive.status_code == 403, destructive.text


@pytest.mark.asyncio
async def test_a_viewer_cannot_suspend(client, make_admin, make_user, mint_token):
    viewer = await make_admin(email="viewer2@example.com", role=AdminRoles.viewer)
    target = await make_user(email="target2@example.com")
    token = await mint_token(viewer)

    response = await client.put(
        f"/api/v1/user/admin/{target.id}/active",
        headers=auth_header(token),
        json={"is_active": False},
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_support_can_moderate_but_not_destroy_or_impersonate(
    client, make_admin, make_user, make_club, mint_token
):
    """The day-to-day role: act on people, do not remove records or take money."""
    support = await make_admin(email="support@example.com", role=AdminRoles.support)
    target = await make_user(email="supporttarget@example.com")
    _, club = await make_club(name="Kept", email="keptclub@example.com")
    token = await mint_token(support)

    suspend = await client.put(
        f"/api/v1/user/admin/{target.id}/active",
        headers=auth_header(token),
        json={"is_active": False, "reason": "spam"},
    )
    assert suspend.status_code == 200, suspend.text

    destroy = await client.delete(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )
    assert destroy.status_code == 403

    impersonate = await client.post(
        f"/api/v1/auth/admin/impersonate/{target.id}", headers=auth_header(token)
    )
    assert impersonate.status_code == 403


@pytest.mark.asyncio
async def test_an_owner_can_do_everything(
    client, make_admin, make_user, make_club, mint_token
):
    owner = await make_admin(email="owner@example.com", role=AdminRoles.owner)
    target = await make_user(email="ownertarget@example.com")
    _, club = await make_club(name="Removable", email="removable@example.com")
    token = await mint_token(owner)

    assert (
        await client.post(
            f"/api/v1/auth/admin/impersonate/{target.id}", headers=auth_header(token)
        )
    ).status_code == 200
    assert (
        await client.delete(
            f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_a_viewer_cannot_refund(client, make_admin, mint_token):
    """Money is its own capability, not bundled with general admin."""
    import uuid

    viewer = await make_admin(email="viewer3@example.com", role=AdminRoles.viewer)
    token = await mint_token(viewer)

    response = await client.post(
        f"/api/v1/payments/logs/{uuid.uuid4()}/refund", headers=auth_header(token)
    )

    # Refused on the role, before the payment is even looked up.
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_a_non_admin_is_still_refused_regardless_of_role(
    client, make_user, mint_token
):
    """The role layer sits on top of AdminAuth; it must not replace it."""
    ordinary = await make_user(email="ordinary@example.com")
    token = await mint_token(ordinary)

    response = await client.get("/api/v1/clubs/admin/list", headers=auth_header(token))

    assert response.status_code in (401, 403), response.text
