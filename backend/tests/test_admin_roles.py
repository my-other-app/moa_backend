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


# ---------------------------------------------------------------------------
# Coverage for the 27 mutating endpoints that previously accepted any admin.
#
# Before this, `admin_role` was enforced on eight endpoints. The UI offered a
# Viewer role described as "Read everything, change nothing" while that viewer
# could delete organisations and badges, because every other handler asked only
# "is this an admin". These assert the boundary per capability rather than per
# endpoint: one representative route each, for the three roles.
#
# A denied call must be exactly 403 -- the guard is a dependency, so it runs
# before the body is validated and before the row is looked up, which is why a
# dummy id is enough. An allowed call is asserted as "not 403": it may well 404
# or 422 on the dummy id, and that still proves the guard let it through.
# ---------------------------------------------------------------------------

MODERATE_ROUTE = ("put", "/api/v1/orgs/block/1", {"is_blocked": True})
DESTROY_ROUTE = ("delete", "/api/v1/orgs/delete/1", None)
CONTENT_ROUTE = ("post", "/api/v1/interests/create", {"name": "X", "category_id": 1})
CREDENTIALS_ROUTE = (
    "post",
    "/api/v1/auth/admin/reset-password",
    {"identifier": "someone", "new_password": "Irrelevant123!"},
)


async def _call(client, token, route):
    method, path, body = route
    kwargs = {"headers": auth_header(token)}
    if body is not None:
        kwargs["json"] = body
    return await getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize(
    "role,route,allowed",
    [
        # A viewer changes nothing at all, which is what the UI promises.
        ("viewer", MODERATE_ROUTE, False),
        ("viewer", DESTROY_ROUTE, False),
        ("viewer", CONTENT_ROUTE, False),
        ("viewer", CREDENTIALS_ROUTE, False),
        # Support moderates -- approve, suspend, award, announce, block.
        ("support", MODERATE_ROUTE, True),
        # ...but does not delete, reshape the catalogue, or take over accounts.
        ("support", DESTROY_ROUTE, False),
        ("support", CONTENT_ROUTE, False),
        ("support", CREDENTIALS_ROUTE, False),
        ("owner", MODERATE_ROUTE, True),
        ("owner", DESTROY_ROUTE, True),
        ("owner", CONTENT_ROUTE, True),
        ("owner", CREDENTIALS_ROUTE, True),
    ],
)
@pytest.mark.asyncio
async def test_capability_boundaries(
    client, make_admin, mint_token, role, route, allowed
):
    admin = await make_admin(email=f"{role}-{route[1].count('/')}@example.com", role=role)
    token = await mint_token(admin)

    response = await _call(client, token, route)

    if allowed:
        assert response.status_code != 403, response.text
    else:
        assert response.status_code == 403, response.text
        assert response.json().get("error_code") == "INSUFFICIENT_ADMIN_ROLE"


@pytest.mark.asyncio
async def test_the_refusal_names_the_capability_not_the_role(
    client, make_admin, mint_token
):
    """
    An admin told only "forbidden" cannot tell whether to ask for a different
    role or stop trying. The message names the missing capability.
    """
    admin = await make_admin(email="namedcap@example.com", role="viewer")
    token = await mint_token(admin)

    response = await _call(client, token, DESTROY_ROUTE)

    assert response.status_code == 403
    assert "destroy" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_creating_content_is_not_reported_as_destroying(
    client, make_admin, mint_token
):
    """
    Catalogue writes were the reason for a capability of their own: routing
    them through DESTROY would have told a support admin that creating an
    interest was a deletion.
    """
    admin = await make_admin(email="contentmsg@example.com", role="support")
    token = await mint_token(admin)

    response = await _call(client, token, CONTENT_ROUTE)

    assert response.status_code == 403
    message = response.json()["message"].lower()
    assert "content" in message and "destroy" not in message


@pytest.mark.asyncio
async def test_me_reports_the_admin_role_as_a_plain_string(
    client, make_admin, mint_token
):
    """
    The dashboard hides actions a role cannot perform, which it can only do if
    the role reaches it. Asserting the exact string matters: a SQLAlchemy Enum
    serialises as "AdminRoles.support" unless it is unwrapped, and the client
    compares against "support".
    """
    admin = await make_admin(email="meresolve@example.com", role="support")
    token = await mint_token(admin)

    response = await client.get("/api/v1/auth/me", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json()["admin_role"] == "support"


@pytest.mark.asyncio
async def test_me_reports_no_role_for_a_non_admin(client, make_user, mint_token):
    user = await make_user(email="plainme@example.com")
    token = await mint_token(user)

    response = await client.get("/api/v1/auth/me", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json().get("admin_role") in (None, "owner")
